"""Standalone ASR/TTS data, independent of the retired server's Python source."""

import json
from pathlib import Path


def load_voice_profile(path: Path) -> dict:
    try:
        if path.stat().st_size > 65_536:
            raise ValueError()

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result

        profile = json.loads(path.read_text("utf-8-sig"), object_pairs_hook=unique)
        if not isinstance(profile, dict) or set(profile) != {"ASRConfig", "TTSConfig"}:
            raise ValueError()
        for value in profile.values():
            if (not isinstance(value, dict) or set(value) != {"Provider", "ProviderParams"}
                    or not isinstance(value["Provider"], str) or not value["Provider"].strip()
                    or not isinstance(value["ProviderParams"], dict)):
                raise ValueError()
        return profile
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid standalone ASR/TTS profile") from None
