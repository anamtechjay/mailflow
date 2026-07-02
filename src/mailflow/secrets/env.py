"""EnvSecretProvider — resolve `env://NAME` references from the process environment.

Implements the core SecretProvider port (`get(ref) -> str`). A bare value with no
`scheme://` is returned verbatim, so a literal secret can be injected directly in
dev. Unknown schemes raise (so a `kv://` ref isn't silently treated as a literal).
Cloud secret stores (Key Vault / GSM) are separate providers behind the same port.
"""

from __future__ import annotations

import os

_ENV_SCHEME = "env://"
_KNOWN_SCHEMES = (_ENV_SCHEME,)


class EnvSecretProvider:
    def get(self, ref: str) -> str:
        if ref.startswith(_ENV_SCHEME):
            name = ref[len(_ENV_SCHEME):]
            try:
                return os.environ[name]
            except KeyError as exc:
                raise KeyError(f"env secret {name!r} not set") from exc
        if "://" in ref and not ref.startswith(_KNOWN_SCHEMES):
            scheme = ref.split("://", 1)[0]
            raise ValueError(f"unsupported secret scheme {scheme!r}: {ref!r}")
        return ref  # literal pass-through
