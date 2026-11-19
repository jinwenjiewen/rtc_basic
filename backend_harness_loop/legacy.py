"""Read only literal ASR/TTS configuration, without importing legacy code."""

from __future__ import annotations

import ast
from pathlib import Path


def load_legacy_voice_profile(source: Path) -> dict:
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    candidates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = {
            key.value: value
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if {"ASRConfig", "TTSConfig"} <= fields.keys():
            try:
                profile = {name: ast.literal_eval(fields[name]) for name in ("ASRConfig", "TTSConfig")}
            except (ValueError, TypeError, SyntaxError) as exc:
                raise ValueError("Legacy voice configuration must contain only literal data") from exc
            if not all(
                isinstance(value, dict)
                and isinstance(value.get("Provider"), str)
                and isinstance(value.get("ProviderParams"), dict)
                for value in profile.values()
            ):
                raise ValueError("Legacy ASR and TTS provider configuration is incomplete")
            candidates.append(profile)
    if len(candidates) != 1:
        raise ValueError("Expected one unambiguous literal ASR/TTS configuration")
    return candidates[0]
