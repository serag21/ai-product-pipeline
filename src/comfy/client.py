"""Local ComfyUI client.

Adapted from the ComfyUI client used by calesthio/OpenMontage:
https://github.com/calesthio/OpenMontage/blob/main/tools/_comfyui/client.py

This file is distributed under the GNU Affero General Public License v3.
"""
from __future__ import annotations

import copy
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import requests


class ComfyUIError(RuntimeError):
    def __init__(self, message: str, prompt_id: str | None = None) -> None:
        super().__init__(message)
        self.prompt_id = prompt_id


class ComfyUIClient:
    """Small client for a locally running ComfyUI server."""

    def __init__(self, server_url: str = "http://127.0.0.1:8188") -> None:
        self.server_url = server_url.rstrip("/")
        self.client_id = str(uuid.uuid4())

    def health(self) -> dict[str, Any]:
        response = requests.get(f"{self.server_url}/system_stats", timeout=10)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def load_workflow(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def patch_workflow(
        workflow: dict[str, Any],
        patches: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        result = copy.deepcopy(workflow)
        for node_id, values in patches.items():
            if node_id not in result:
                raise ComfyUIError(f"Workflow node {node_id!r} was not found.")
            inputs = result[node_id].get("inputs")
            if not isinstance(inputs, dict):
                raise ComfyUIError(
                    f"Workflow node {node_id!r} has no API-format inputs object."
                )
            for key, value in values.items():
                if key not in inputs:
                    raise ComfyUIError(
                        f"Workflow node {node_id!r} has no input named {key!r}."
                    )
                inputs[key] = value
        return result

    @staticmethod
    def random_seed() -> int:
        return random.randint(0, 2**32 - 1)

    def submit(self, workflow: dict[str, Any]) -> str:
        response = requests.post(
            f"{self.server_url}/prompt",
            json={"prompt": workflow, "client_id": self.client_id},
            timeout=30,
        )
        payload = response.json()
        if payload.get("node_errors"):
            raise ComfyUIError(f"ComfyUI node errors: {payload['node_errors']}")
        if not response.ok:
            raise ComfyUIError(f"ComfyUI rejected workflow: {payload}")
        prompt_id = payload.get("prompt_id")
        if not prompt_id:
            raise ComfyUIError(f"ComfyUI returned no prompt_id: {payload}")
        return prompt_id

    def _history_entry(self, prompt_id: str) -> dict[str, Any] | None:
        response = requests.get(
            f"{self.server_url}/history/{prompt_id}",
            timeout=10,
        )
        response.raise_for_status()
        entry = response.json().get(prompt_id)
        if entry is None:
            return None

        status = entry.get("status", {})
        if status.get("status_str") == "error":
            raise ComfyUIError(
                f"ComfyUI execution failed: {status.get('messages', [])}",
                prompt_id,
            )
        return entry

    def wait(
        self,
        prompt_id: str,
        timeout: int = 900,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Wait for completion, preferring WebSocket events with REST fallback."""
        started = time.time()

        try:
            import websocket

            ws_url = self.server_url.replace("https://", "wss://").replace(
                "http://", "ws://"
            )
            connection = websocket.create_connection(
                f"{ws_url}/ws?clientId={self.client_id}",
                timeout=10,
            )
            connection.settimeout(5)

            try:
                while time.time() - started < timeout:
                    try:
                        raw = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        entry = self._history_entry(prompt_id)
                        if entry is not None:
                            return entry
                        continue

                    if not isinstance(raw, str):
                        continue

                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    data = message.get("data", {})
                    message_prompt_id = data.get("prompt_id")
                    if message_prompt_id not in (None, prompt_id):
                        continue

                    if message.get("type") == "progress" and on_progress:
                        on_progress(data)

                    if (
                        message.get("type") == "execution_error"
                        and message_prompt_id == prompt_id
                    ):
                        raise ComfyUIError(
                            f"ComfyUI execution error: {data}",
                            prompt_id,
                        )

                    if (
                        message.get("type") == "executing"
                        and data.get("node") is None
                        and message_prompt_id == prompt_id
                    ):
                        entry = self._history_entry(prompt_id)
                        if entry is None:
                            raise ComfyUIError(
                                "ComfyUI reported completion but history is unavailable.",
                                prompt_id,
                            )
                        return entry
            finally:
                connection.close()

        except ComfyUIError:
            raise
        except Exception:
            # Transport failure: use authoritative history polling.
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            entry = self._history_entry(prompt_id)
            if entry is not None:
                return entry
            time.sleep(2)

        raise ComfyUIError(
            f"Prompt {prompt_id} did not complete within {timeout}s.",
            prompt_id,
        )

    def download_image(
        self,
        image_info: dict[str, Any],
        destination: Path,
    ) -> Path:
        response = requests.get(
            f"{self.server_url}/view",
            params={
                "filename": image_info["filename"],
                "subfolder": image_info.get("subfolder", ""),
                "type": image_info.get("type", "output"),
            },
            timeout=120,
        )
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return destination

    def generate(
        self,
        workflow: dict[str, Any],
        output_node: str,
        destination: Path,
        timeout: int = 900,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[Path]:
        prompt_id = self.submit(workflow)
        history = self.wait(
            prompt_id,
            timeout=timeout,
            on_progress=on_progress,
        )

        node_output = history.get("outputs", {}).get(output_node, {})
        images = node_output.get("images", [])
        if not images:
            raise ComfyUIError(
                f"No image output found on node {output_node}. "
                f"Available outputs: {list(history.get('outputs', {}).keys())}",
                prompt_id,
            )

        paths: list[Path] = []
        for index, image_info in enumerate(images):
            path = destination
            if len(images) > 1:
                path = destination.with_name(
                    f"{destination.stem}_{index:03d}{destination.suffix}"
                )
            paths.append(self.download_image(image_info, path))
        return paths
