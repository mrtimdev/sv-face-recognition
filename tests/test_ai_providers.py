"""AI provider requests and parsing, with no network access."""
import base64
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from face_attendance import ai_providers as ai
from face_attendance.ai_providers import AIConfig, AIError
from face_attendance.settings import Settings

KEY = "sk-secret-key-123"


def jpeg(width=64, height=64):
    return cv2.imencode(".jpg", np.full((height, width, 3), 120, np.uint8))[1].tobytes()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(status, body=b""):
    return urllib.error.HTTPError("https://x", status, "error", {}, io.BytesIO(body))


class FakeHTTP:
    """Replays answers in order and records each request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout, follow_redirects=False):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer if isinstance(answer, bytes) else json.dumps(answer).encode())


def serve(*answers):
    return patch.object(ai, "_open", FakeHTTP(*answers))


def header(request, name):
    return dict((key.lower(), value) for key, value in request.header_items()).get(name.lower())


class ModelTests(unittest.TestCase):
    def test_openai_lists_sorted_models_with_a_bearer_key(self):
        with serve({"data": [{"id": "gpt-b"}, {"id": "gpt-a"}]}) as http:
            models = ai.list_models(AIConfig("openai", KEY))
        self.assertEqual(models, ["gpt-a", "gpt-b"])
        self.assertEqual(http.requests[0].full_url, "https://api.openai.com/v1/models")
        self.assertEqual(header(http.requests[0], "Authorization"), f"Bearer {KEY}")

    def test_gemini_keeps_generate_content_models_and_never_puts_the_key_in_the_url(self):
        answer = {"models": [
            {"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embed-y", "supportedGenerationMethods": ["embedContent"]}]}
        with serve(answer) as http:
            self.assertEqual(ai.list_models(AIConfig("gemini", KEY)), ["gemini-x"])
        self.assertNotIn(KEY, http.requests[0].full_url)
        self.assertEqual(header(http.requests[0], "x-goog-api-key"), KEY)

    def test_claude_sends_key_and_version_headers(self):
        with serve({"data": [{"id": "claude-opus-5-5"}]}) as http:
            self.assertEqual(ai.list_models(AIConfig("claude", KEY)), ["claude-opus-5-5"])
        self.assertEqual(header(http.requests[0], "x-api-key"), KEY)
        self.assertEqual(header(http.requests[0], "anthropic-version"), ai.ANTHROPIC_VERSION)

    def test_custom_uses_its_own_base_url_and_ignores_a_trailing_slash(self):
        with serve({"data": [{"id": "m"}]}) as http:
            ai.list_models(AIConfig("custom", KEY, base_url="https://gateway.example/v1/"))
        self.assertEqual(http.requests[0].full_url, "https://gateway.example/v1/models")

    def test_custom_without_a_key_is_allowed_for_local_servers(self):
        with serve({"data": [{"id": "local"}]}) as http:
            ai.list_models(AIConfig("custom", base_url="http://localhost:11434/v1"))
        self.assertIsNone(header(http.requests[0], "Authorization"))

    def test_plain_http_custom_url_works_and_warns_only_for_public_hosts(self):
        with serve({"data": [{"id": "m"}]}) as http:
            ai.list_models(AIConfig("custom", KEY, base_url="http://45.201.196.19:3003/v1"))
        self.assertEqual(http.requests[0].full_url, "http://45.201.196.19:3003/v1/models")
        self.assertIn("unencrypted", ai.plain_http_warning("http://45.201.196.19:3003/v1"))
        for safe in ("https://gateway.example/v1", "http://localhost:11434/v1", "http://192.168.1.5/v1", ""):
            self.assertEqual(ai.plain_http_warning(safe), "")

    def test_builtin_provider_needs_a_key_before_any_request(self):
        with serve() as http, self.assertRaisesRegex(AIError, "API key"):
            ai.list_models(AIConfig("openai"))
        self.assertEqual(http.requests, [])

    def test_split_models_separates_image_models_and_falls_back(self):
        chat, image = ai.split_models(["gpt-x", "gpt-image-y", "text-embedding-z", "whisper-1"])
        self.assertEqual((chat, image), (["gpt-x"], ["gpt-image-y"]))
        self.assertEqual(ai.split_models(["only"]), (["only"], ["only"]))


class ReportedModelTypeTests(unittest.TestCase):
    ANSWER = {"data": [
        {"id": "z/gemini-3-flash-image", "type": "image", "output_modalities": ["image"], "input_modalities": ["text"]},
        {"id": "a/plain-name", "type": "image", "output_modalities": ["image"], "input_modalities": ["text", "image"]},
        {"id": "c/chat", "output_modalities": ["text"], "input_modalities": ["text"]},
        {"id": "v/video", "type": "video", "output_modalities": ["video"]},
        {"id": "u/unlabelled"},
        {"id": "u/image-unlabelled"},
    ]}

    def test_reported_types_win_over_names_and_photo_editors_come_first(self):
        with serve(self.ANSWER):
            models = ai.list_models(AIConfig("custom", KEY, "https://g.example/v1"))
        self.assertIsInstance(models, list)
        chat, image = ai.split_models(models)
        self.assertEqual(chat, ["c/chat", "u/unlabelled"])
        self.assertEqual(image, ["z/gemini-3-flash-image", "a/plain-name", "u/image-unlabelled"])


class TestMessageTests(unittest.TestCase):
    def test_openai_compatible_reply(self):
        answer = {"choices": [{"message": {"content": " Hello "}}]}
        with serve(answer) as http:
            reply = ai.send_test_message(AIConfig("custom", KEY, "https://g.example/v1", chat_model="m"), "Hi")
        self.assertEqual(reply, "Hello")
        body = json.loads(http.requests[0].data)
        self.assertEqual((body["model"], body["messages"][0]["content"]), ("m", "Hi"))

    def test_gemini_reply(self):
        answer = {"candidates": [{"content": {"parts": [{"text": "Hi there"}]}}]}
        with serve(answer) as http:
            self.assertEqual(ai.send_test_message(AIConfig("gemini", KEY, chat_model="models/g-1")), "Hi there")
        self.assertTrue(http.requests[0].full_url.endswith("/models/g-1:generateContent"))

    def test_claude_reply_skips_thinking_blocks(self):
        answer = {"stop_reason": "end_turn",
                  "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Hello"}]}
        with serve(answer) as http:
            self.assertEqual(ai.send_test_message(AIConfig("claude", KEY, chat_model="claude-opus-5-5")), "Hello")
        body = json.loads(http.requests[0].data)
        self.assertNotIn("temperature", body)
        self.assertEqual(http.requests[0].full_url, "https://api.anthropic.com/v1/messages")

    def test_claude_refusal_is_reported(self):
        with serve({"stop_reason": "refusal", "content": []}), self.assertRaisesRegex(AIError, "declined"):
            ai.send_test_message(AIConfig("claude", KEY, chat_model="claude-opus-5-5"))

    def test_chat_model_is_required(self):
        with serve() as http, self.assertRaisesRegex(AIError, "chat model"):
            ai.send_test_message(AIConfig("openai", KEY))
        self.assertEqual(http.requests, [])

    def test_errors_are_friendly_and_never_contain_the_key(self):
        body = json.dumps({"error": {"message": f"Incorrect API key {KEY}"}}).encode()
        with serve(http_error(401, body)), self.assertRaises(AIError) as caught:
            ai.list_models(AIConfig("openai", KEY))
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("rejected the API key", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_unreachable_host_and_non_json_answers_are_explained(self):
        with serve(urllib.error.URLError("no route")), self.assertRaisesRegex(AIError, "api.openai.com"):
            ai.list_models(AIConfig("openai", KEY))
        with serve(b"<html>login</html>"), self.assertRaisesRegex(AIError, "base URL"):
            ai.list_models(AIConfig("openai", KEY))

    def test_redirects_are_not_followed_with_the_key(self):
        self.assertIsNone(ai._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))
        with serve(http_error(302)), self.assertRaisesRegex(AIError, "redirected"):
            ai.list_models(AIConfig("openai", KEY))


class ImageTests(unittest.TestCase):
    SOURCE = jpeg()

    def test_claude_refuses_without_a_request(self):
        with serve() as http, self.assertRaisesRegex(AIError, "can't generate images"):
            ai.generate_image(AIConfig("claude", KEY, image_model="x"), self.SOURCE, "prompt")
        self.assertEqual(http.requests, [])

    def test_openai_sends_the_photo_and_high_fidelity(self):
        answer = {"data": [{"b64_json": base64.b64encode(b"image").decode()}]}
        with serve(answer) as http:
            data = ai.generate_image(AIConfig("openai", KEY, image_model="gpt-image-1"), self.SOURCE, "prompt")
        self.assertEqual(data, b"image")
        request = http.requests[0]
        self.assertTrue(request.full_url.endswith("/images/edits"))
        self.assertIn(self.SOURCE, request.data)
        self.assertIn(b'name="input_fidelity"', request.data)
        self.assertIn(b"1024x1024", request.data)

    def test_openai_retries_without_fidelity_when_the_model_rejects_it(self):
        rejected = http_error(400, json.dumps({"error": {"message": "Unknown parameter: input_fidelity"}}).encode())
        answer = {"data": [{"b64_json": base64.b64encode(b"ok").decode()}]}
        with serve(rejected, answer) as http:
            ai.generate_image(AIConfig("openai", KEY, image_model="gpt-image-mini"), self.SOURCE, "p")
        self.assertNotIn(b"input_fidelity", http.requests[1].data)

    def test_download_links_do_not_receive_the_key(self):
        answer = {"data": [{"url": "https://cdn.example/a.png"}]}
        with serve(answer, b"picture") as http:
            data = ai.generate_image(AIConfig("custom", KEY, "https://g.example/v1", image_model="m"),
                                     self.SOURCE, "p")
        self.assertEqual(data, b"picture")
        self.assertIsNone(header(http.requests[1], "Authorization"))

    def test_custom_falls_back_to_chat_when_images_endpoint_is_missing(self):
        url = "data:image/png;base64," + base64.b64encode(b"chat-image").decode()
        answer = {"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]}
        with serve(http_error(404), answer) as http:
            data = ai.generate_image(AIConfig("custom", KEY, "https://g.example/v1", image_model="m"),
                                     self.SOURCE, "p")
        self.assertEqual(data, b"chat-image")
        self.assertTrue(http.requests[1].full_url.endswith("/chat/completions"))

    def test_custom_chat_text_only_is_an_error(self):
        answer = {"choices": [{"message": {"content": "I can only write text."}}]}
        with serve(http_error(404), answer), self.assertRaisesRegex(AIError, "text instead of an image"):
            ai.generate_image(AIConfig("custom", KEY, "https://g.example/v1", image_model="m"), self.SOURCE, "p")

    def test_gemini_image_and_blocks(self):
        config = AIConfig("gemini", KEY, image_model="gemini-image")
        answer = {"candidates": [{"content": {"parts": [
            {"text": "here"}, {"inlineData": {"data": base64.b64encode(b"g").decode()}}]}}]}
        with serve(answer) as http:
            self.assertEqual(ai.generate_image(config, self.SOURCE, "p"), b"g")
        body = json.loads(http.requests[0].data)
        self.assertEqual(body["generationConfig"]["imageConfig"]["aspectRatio"], "1:1")
        with serve({"promptFeedback": {"blockReason": "SAFETY"}}), self.assertRaisesRegex(AIError, "SAFETY"):
            ai.generate_image(config, self.SOURCE, "p")

    def test_gemini_retries_without_image_config_on_older_models(self):
        config = AIConfig("gemini", KEY, image_model="old")
        rejected = http_error(400, json.dumps({"error": {"message": "Unknown name image_config"}}).encode())
        answer = {"candidates": [{"content": {"parts": [{"inlineData": {"data": "Zw=="}}]}}]}
        with serve(rejected, answer) as http:
            ai.generate_image(config, self.SOURCE, "p")
        self.assertNotIn("imageConfig", json.loads(http.requests[1].data)["generationConfig"])

    def test_results_are_cropped_square_and_saved_privately(self):
        config = AIConfig("openai", KEY, image_model="gpt-image-1")
        answer = {"data": [{"b64_json": base64.b64encode(jpeg(200, 100)).decode()}]}
        with tempfile.TemporaryDirectory() as directory, serve(answer):
            path = ai.generate_angle_file(config, self.SOURCE, ai.ANGLES[0], Path(directory) / "run")
            image = cv2.imread(str(path))
            self.assertEqual(image.shape[:2], (100, 100))
            self.assertEqual(path.name, "left_30.jpg")

    def test_unreadable_results_are_rejected(self):
        with self.assertRaisesRegex(AIError, "readable image"):
            ai.square_image(b"not an image")

    def test_large_sources_are_scaled_down(self):
        encoded = ai.encode_source(np.zeros((3000, 1500, 3), np.uint8))
        decoded = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(max(decoded.shape[:2]), ai.MAX_SOURCE_SIDE)

    def test_six_distinct_angles_with_general_prompts(self):
        self.assertEqual(len({angle.key for angle in ai.ANGLES}), 6)
        prompt = ai.angle_prompt(ai.ANGLES[2])
        self.assertIn(ai.ANGLES[2].instruction, prompt)
        self.assertIn("1:1", prompt)
        self.assertNotIn(" man", prompt)


class StoredConfigurationTests(unittest.TestCase):
    def test_each_provider_keeps_its_own_key(self):
        settings = Settings()
        profiles = ai.load_profiles(settings)
        profiles["openai"]["api_key"] = "open-key"
        profiles["gemini"]["api_key"] = "gem-key"
        settings.ai_profiles = ai.dump_profiles(profiles)
        settings.ai_provider = "gemini"
        self.assertEqual(ai.config_from_settings(settings).api_key, "gem-key")
        settings.ai_provider = "claude"
        self.assertEqual(ai.config_from_settings(settings).api_key, "")

    def test_damaged_or_unknown_values_fall_back_safely(self):
        settings = Settings(ai_provider="nope", ai_profiles="{broken")
        config = ai.config_from_settings(settings)
        self.assertEqual((config.provider, config.api_key), ("openai", ""))

    def test_settings_survive_a_save_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = Settings(ai_provider="custom", ai_profiles=ai.dump_profiles(
                {"custom": {"api_key": "k", "base_url": "https://g.example/v1"}}))
            settings.save(path)
            config = ai.config_from_settings(Settings.load(path))
        self.assertEqual((config.provider, config.base_url, config.api_key), ("custom", "https://g.example/v1", "k"))

    def test_key_is_not_in_the_config_repr(self):
        self.assertNotIn(KEY, repr(AIConfig("openai", KEY)))

    def test_configuration_problems_name_what_is_missing(self):
        self.assertIn("can't generate", ai.configuration_problem(AIConfig("claude", KEY)))
        self.assertIn("API key", ai.configuration_problem(AIConfig("openai")))
        self.assertIn("base URL", ai.configuration_problem(AIConfig("custom")))
        self.assertIn("image model", ai.configuration_problem(AIConfig("openai", KEY)))
        self.assertEqual(ai.configuration_problem(AIConfig("openai", KEY, image_model="m")), "")


if __name__ == "__main__":
    unittest.main()
