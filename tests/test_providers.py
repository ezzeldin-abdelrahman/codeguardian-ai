import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codeguardian.providers import get_settings, review_code


class ProviderTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "AI_PROVIDER": "gemini",
            "GEMINI_MODEL": "gemini-default",
            "GEMINI_API_KEY": "fake-gemini-key",
            "OPENAI_MODEL": "openai-default",
            "OPENAI_API_KEY": "fake-openai-key",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_defaults_and_cli_overrides(self):
        self.assertEqual(get_settings(), ("gemini", "gemini-default", "fake-gemini-key"))
        self.assertEqual(get_settings("openai"), ("openai", "openai-default", "fake-openai-key"))
        self.assertEqual(get_settings(model="alternative")[1], "alternative")
        self.assertEqual(os.environ["AI_PROVIDER"], "gemini")

    def test_multiple_models_and_provider_specific_validation(self):
        os.environ["GEMINI_MODELS"] = "gemini-default, alternative"
        os.environ["OPENAI_MODELS"] = "openai-default"
        self.assertEqual(get_settings(model="alternative")[1], "alternative")
        with self.assertRaisesRegex(ValueError, "OPENAI_MODELS"):
            get_settings("openai", "alternative")
        os.environ["GEMINI_MODELS"] = "alternative"
        with self.assertRaisesRegex(ValueError, "GEMINI_MODELS"):
            get_settings()

    def test_disabled_and_unknown_providers(self):
        os.environ["SUPPORTED_PROVIDERS"] = "gemini"
        with self.assertRaises(ValueError):
            get_settings("openai")
        with self.assertRaises(ValueError):
            get_settings("unknown")

    def test_only_selected_key_is_required(self):
        del os.environ["OPENAI_API_KEY"]
        self.assertEqual(get_settings()[0], "gemini")
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            get_settings("openai")

    def test_missing_default_and_model(self):
        del os.environ["AI_PROVIDER"]
        with self.assertRaises(ValueError):
            get_settings()
        del os.environ["GEMINI_MODEL"]
        with self.assertRaisesRegex(ValueError, "GEMINI_MODEL"):
            get_settings("gemini")

    def test_gemini_request(self):
        with patch("google.genai.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.models.generate_content.return_value = SimpleNamespace(text="Gemini review")
            self.assertEqual(review_code("x = 1", "gemini", "chosen", "fake"), "Gemini review")
            arguments = client.models.generate_content.call_args.kwargs
            self.assertEqual(arguments["model"], "chosen")
            self.assertEqual(arguments["contents"], "x = 1")
            self.assertIn("Review", arguments["config"].system_instruction)
            self.assertTrue(arguments["config"].automatic_function_calling.disable)

    def test_gemini_api_error_reports_reason_and_redacts_key(self):
        from google.genai.errors import APIError

        error = APIError(429, {"error": {"message": "Quota exceeded for secret-key"}})
        with patch("google.genai.Client", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                review_code("x = 1", "gemini", "chosen", "secret-key")
        self.assertIn("429: Quota exceeded", str(caught.exception))
        self.assertNotIn("secret-key", str(caught.exception))

    def test_openai_request_and_empty_response(self):
        with patch("openai.OpenAI") as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.create.return_value = SimpleNamespace(output_text="OpenAI review")
            self.assertEqual(review_code("x = 1", "openai", "chosen", "fake"), "OpenAI review")
            arguments = client.responses.create.call_args.kwargs
            self.assertEqual(arguments["model"], "chosen")
            self.assertEqual(arguments["input"], "x = 1")
            client.responses.create.return_value = SimpleNamespace(output_text="")
            with self.assertRaisesRegex(RuntimeError, "no review text"):
                review_code("x = 1", "openai", "chosen", "fake")

    def test_network_errors_do_not_expose_credentials(self):
        import httpx
        from openai import APIConnectionError

        for provider, target, error in [
            ("gemini", "google.genai.Client", httpx.ConnectError("secret-key")),
            ("openai", "openai.OpenAI", APIConnectionError(
                message="secret-key", request=httpx.Request("POST", "https://example.com")
            )),
        ]:
            with self.subTest(provider=provider), patch(target, side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    review_code("x = 1", provider, "chosen", "secret-key")
                self.assertNotIn("secret-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
