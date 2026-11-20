"""Download the search model ahead of time, so meaning-based search is ready the moment the
bot, the eval runner or the tests start (otherwise it downloads on first use).

    python -m app.download_model

It goes into data/models (about 70 MB, once). Safe to run while Sprinkle is running.
"""

import sys
import time

from app.config import Settings
from app.services import semantic


def main() -> int:
    settings = Settings()
    folder = semantic.model_dir(settings)
    start = time.monotonic()
    try:
        model = semantic.load_model(settings)
    except Exception as exc:  # noqa: BLE001 - say what failed in plain words
        print(f"Couldn't get {settings.embedding_model} into {folder}: {type(exc).__name__}: {exc}"[:500])
        print("It downloads from Hugging Face (huggingface.co), so that site has to be reachable.")
        return 1
    print(f"Search model ready: {settings.embedding_model} ({model.dim} numbers per text) in {folder}, "
          f"{time.monotonic() - start:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
