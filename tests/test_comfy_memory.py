import unittest
from unittest.mock import Mock, patch

from src.comfy.client import ComfyUIClient


class ComfyMemoryTests(unittest.TestCase):
    def test_free_memory_requests_unload_and_checks_available_vram(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        health = {"devices": [{"type": "cuda", "vram_total": 32 * 1024**3,
                               "vram_free": 28 * 1024**3}]}
        with patch("src.comfy.client.requests.post", return_value=response) as post, \
                patch.object(ComfyUIClient, "health", return_value=health):
            ComfyUIClient().free_memory()
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:8188/free")
        self.assertEqual(post.call_args.kwargs["json"], {"unload_models": True, "free_memory": True})

    def test_free_memory_falls_back_to_namespaced_api_route(self):
        not_found = Mock(status_code=404)
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        health = {"devices": [{"type": "cuda", "vram_total": 32 * 1024**3,
                               "vram_free": 28 * 1024**3}]}
        with patch("src.comfy.client.requests.post", side_effect=[not_found, response]) as post, \
                patch.object(ComfyUIClient, "health", return_value=health):
            ComfyUIClient().free_memory()
        self.assertEqual([call.args[0] for call in post.call_args_list],
                         ["http://127.0.0.1:8188/free", "http://127.0.0.1:8188/api/free"])


if __name__ == "__main__":
    unittest.main()
