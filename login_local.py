#!/usr/bin/env python3
"""Genera el token de Garmin desde tu red local (donde Garmin no bloquea la IP).

Uso:
  python login_local.py
  -> te pide email + password (y MFA si lo tienes)
  -> escribe el JSON de tokens en ./garmin_tokens.json
  -> imprime la variable GARMIN_TOKENS_JSON=... lista para pegar en Dokploy

Después en Dokploy:
  - Opción A (recomendada): copia el JSON a /data/garmin/garmin_tokens.json
    desde la terminal del contenedor.
  - Opción B: pega GARMIN_TOKENS_JSON=<base64> como variable de entorno.
"""
import base64
import json
import tempfile
from pathlib import Path
from getpass import getpass

from garminconnect import Garmin


def main() -> None:
    email = input("Email de Garmin: ").strip()
    password = getpass("Contraseña de Garmin: ")

    mfa_holder: dict[str, str] = {}

    def prompt_mfa() -> str:
        return input("Código MFA (si te lo pide Garmin, introduce el código): ").strip()

    out_dir = Path("garmin_tokens.json")

    with tempfile.TemporaryDirectory() as tmpdir:
        client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
        client.login(tmpdir)
        token_path = Path(tmpdir) / "garmin_tokens.json"
        if not token_path.exists():
            raise SystemExit("No se generó garmin_tokens.json")
        tokens_text = token_path.read_text(encoding="utf-8")

    # Validar
    try:
        json.loads(tokens_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Tokens no válidos: {exc}")

    # Guardar JSON crudo
    out_dir.write_text(tokens_text, encoding="utf-8")
    print(f"\n✅ Tokens guardados en ./garmin_tokens.json")

    # Generar base64 para variable de entorno
    b64 = base64.b64encode(tokens_text.encode("utf-8")).decode("ascii")
    print(f"\nPara Dokploy (variable de entorno GARMIN_TOKENS_JSON):")
    print(f"GARMIN_TOKENS_JSON={b64}")
    print(f"\nO copia este JSON a /data/garmin/garmin_tokens.json en el contenedor:\n")
    print(tokens_text)


if __name__ == "__main__":
    main()
