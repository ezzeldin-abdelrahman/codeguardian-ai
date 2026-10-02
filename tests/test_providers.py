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

    def test_structured_findings_validation_and_display(self):
        import json
        import io
        from contextlib import redirect_stdout
        from codeguardian.reviewer import parse_findings, print_findings

        finding = dict(category="bug", severity="high", title="Division by zero",
                       line=2, description="The divisor may be zero.", recommendation="Check the divisor.")
        for line in (2, None):
            finding["line"] = line
            result = parse_findings(json.dumps({"findings": [finding]}))
            self.assertEqual(result, [finding])
            output = io.StringIO()
            with redirect_stdout(output):
                print_findings(result)
            self.assertIn("1. [HIGH] BUG", output.getvalue())
            self.assertIn("Line: " + ("2" if line else "Not specified"), output.getvalue())
        self.assertEqual(parse_findings('{"findings": []}'), [])
        output = io.StringIO()
        with redirect_stdout(output):
            print_findings([])
        self.assertEqual(output.getvalue(), "No findings reported.\n")

        for text in ("not JSON", '```json\n{"findings": []}\n```', '{"findings":'):
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                parse_findings(text)
        for value in ([], None, {}, {"findings": {}}, {"findings": [None]}):
            with self.assertRaises(ValueError):
                parse_findings(json.dumps(value))
        for field in finding:
            incomplete = finding.copy()
            del incomplete[field]
            with self.assertRaisesRegex(ValueError, "missing"):
                parse_findings(json.dumps({"findings": [incomplete]}))
        for field, values in {
            "category": ["unknown", [], None], "severity": ["urgent", {}, 2],
            "line": [True, False, 0, -1, 1.5, "2"],
            "title": ["", "  ", 5], "description": [None, ""],
            "recommendation": [[], ""],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    parse_findings(json.dumps({"findings": [dict(finding, **{field: value})]}))

    def test_invalid_json_is_reported_by_cli(self):
        import io
        from contextlib import redirect_stderr
        from codeguardian import reviewer

        output = io.StringIO()
        with patch.object(sys, "argv", ["reviewer.py", __file__]), \
             patch.object(reviewer, "load_dotenv"), \
             patch.object(reviewer, "get_settings", return_value=("groq", "chosen", "fake")), \
             patch.object(reviewer, "review_code", return_value="invalid JSON"), \
             redirect_stderr(output):
            with self.assertRaises(SystemExit) as caught:
                reviewer.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Error: The model returned invalid JSON", output.getvalue())

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
            self.assertEqual(arguments["contents"], "1: x = 1")
            self.assertIn("Review", arguments["config"].system_instruction)
            self.assertTrue(arguments["config"].automatic_function_calling.disable)

    def test_new_provider_settings_and_free_restriction(self):
        for provider, model in [("groq", "openai/gpt-oss-120b"), ("openrouter", "openrouter/free")]:
            os.environ[f"{provider.upper()}_MODEL"] = model
            os.environ[f"{provider.upper()}_API_KEY"] = "fake"
            self.assertEqual(get_settings(provider), (provider, model, "fake"))
        self.assertEqual(get_settings("openrouter", "example/model:free")[1], "example/model:free")
        with self.assertRaisesRegex(ValueError, "requires"):
            get_settings("openrouter", "paid/model")
        with patch("openai.OpenAI") as factory:
            with self.assertRaisesRegex(ValueError, "requires"):
                review_code("x = 1", "openrouter", "paid/model", "fake")
            factory.assert_not_called()

    def test_new_provider_routing_and_empty_response(self):
        for provider, endpoint, model in [
            ("groq", "https://api.groq.com/openai/v1", "chosen"),
            ("openrouter", "https://openrouter.ai/api/v1", "openrouter/free"),
        ]:
            with self.subTest(provider=provider), patch("openai.OpenAI") as factory:
                client = factory.return_value.__enter__.return_value
                client.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Review"))])
                self.assertEqual(review_code("x = 1", provider, model, "fake"), "Review")
                self.assertEqual(factory.call_args.kwargs["base_url"], endpoint)
                self.assertEqual(factory.call_args.kwargs["api_key"], "fake")
                args = client.chat.completions.create.call_args.kwargs
                self.assertEqual(args["model"], model)
                self.assertEqual(args["messages"][1], {"role": "user", "content": "1: x = 1"})
                client.responses.create.assert_not_called()
                client.chat.completions.create.return_value = SimpleNamespace(choices=[])
                with self.assertRaisesRegex(RuntimeError, "no review text"):
                    review_code("x = 1", provider, model, "fake")

    def test_new_provider_errors_use_correct_name(self):
        import httpx
        from openai import APIStatusError

        error = APIStatusError("Invalid secret-key", response=httpx.Response(
            401, request=httpx.Request("POST", "https://example.com")), body=None)
        for provider, label, model in [("groq", "Groq", "chosen"),
                                       ("openrouter", "OpenRouter", "openrouter/free")]:
            with patch("openai.OpenAI", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    review_code("x = 1", provider, model, "secret-key")
                self.assertIn(f"{label} API error 401", str(caught.exception))
                self.assertNotIn("secret-key", str(caught.exception))

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
            self.assertEqual(arguments["input"], "1: x = 1")
            client.responses.create.return_value = SimpleNamespace(output_text="")
            with self.assertRaisesRegex(RuntimeError, "no review text"):
                review_code("x = 1", "openai", "chosen", "fake")

    def test_openai_status_errors_report_reason_and_redact_key(self):
        import httpx
        from openai import APIStatusError

        for status, reason in [(401, "Invalid key"), (404, "Model not found"),
                               (429, "Quota exceeded"), (503, "Service unavailable")]:
            response = httpx.Response(status, request=httpx.Request("POST", "https://example.com"))
            error = APIStatusError("Request failed", response=response,
                                   body={"message": f"{reason}: secret-key"})
            with self.subTest(status=status), patch("openai.OpenAI", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    review_code("x = 1", "openai", "chosen", "secret-key")
                self.assertIn(f"{status}: {reason}", str(caught.exception))
                self.assertNotIn("secret-key", str(caught.exception))

    def test_openai_timeout_is_distinct_from_connection_error(self):
        import httpx
        from openai import APITimeoutError

        error = APITimeoutError(httpx.Request("POST", "https://example.com"))
        with patch("openai.OpenAI", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
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
