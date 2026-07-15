#!/usr/bin/env python3
"""
Surveillance du stock Midea PortaSplit 12 000 BTU en France.

- Utilise Playwright pour rendre les pages comme un navigateur.
- Détecte les passages "indisponible" -> "disponible".
- Envoie une alerte e-mail par SMTP.
- Conserve l'état dans state.json pour éviter les alertes répétées.

Modèle surveillé :
MMCS-12HRN8-QRD0 — EAN 8431312260509
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "sites.json"
STATE_PATH = BASE_DIR / "state.json"

PRODUCT_MODEL = "MMCS-12HRN8-QRD0"
PRODUCT_EAN = "8431312260509"

NORMALIZE_SPACES = re.compile(r"\s+")
DATE_PATTERN = re.compile(
    r"(?:disponible|livraison|retrait|retour en stock|réapprovisionnement|précommande)"
    r".{0,100}?(?:à partir du|dès le|prévu(?:e)?(?: pour)?|le)?\s*"
    r"(?:\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)"
    r"(?:\s+20\d{2})?|\d{1,2}/\d{1,2}/20\d{2})",
    re.IGNORECASE,
)


@dataclass
class CheckResult:
    store: str
    url: str
    status: str  # available, unavailable, preorder, unknown, error
    detail: str
    excerpt: str
    signature: str
    checked_at: str
    error: str | None = None


def normalize(value: str) -> str:
    return NORMALIZE_SPACES.sub(" ", value or "").strip()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json_if_changed(path: Path, data: Any) -> bool:
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == rendered:
        return False
    path.write_text(rendered, encoding="utf-8")
    return True


def text_excerpt(text: str, terms: list[str], radius: int = 180) -> str:
    low = text.lower()
    for term in terms:
        idx = low.find(term.lower())
        if idx >= 0:
            return normalize(text[max(0, idx - radius): idx + len(term) + radius])
    return normalize(text[:500])


def match_any(text: str, phrases: list[str]) -> str | None:
    lower = text.lower()
    for phrase in phrases:
        if phrase.lower() in lower:
            return phrase
    return None


def find_restock_detail(text: str) -> str | None:
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    return normalize(match.group(0))


def classify(text: str, page_html: str, config: dict[str, Any]) -> tuple[str, str, str]:
    visible = normalize(text)
    lower = visible.lower()
    html_lower = page_html.lower()

    # Données structurées, lorsqu'elles sont présentes.
    schema_in_stock = (
        "schema.org/instock" in html_lower
        or '"availability":"instock"' in html_lower
        or '"availability": "instock"' in html_lower
    )
    schema_out_stock = (
        "schema.org/outofstock" in html_lower
        or '"availability":"outofstock"' in html_lower
        or '"availability": "outofstock"' in html_lower
    )
    schema_preorder = (
        "schema.org/preorder" in html_lower
        or '"availability":"preorder"' in html_lower
        or '"availability": "preorder"' in html_lower
    )

    negative = match_any(lower, config.get("negative_phrases", []))
    preorder = match_any(lower, config.get("preorder_phrases", []))
    positive = match_any(lower, config.get("positive_phrases", []))
    restock = find_restock_detail(visible)

    # Les mentions explicites d'indisponibilité ont priorité sur les boutons
    # résiduels "Ajouter au panier".
    if schema_out_stock or negative:
        detail = f"Indisponible : {negative or 'donnée structurée OutOfStock'}"
        if restock:
            detail += f" — {restock}"
        return "unavailable", detail, text_excerpt(visible, [negative or "indisponible"])

    if schema_preorder or preorder:
        detail = f"Précommande : {preorder or 'donnée structurée PreOrder'}"
        if restock:
            detail += f" — {restock}"
        return "preorder", detail, text_excerpt(visible, [preorder or "précommande"])

    if schema_in_stock:
        return (
            "available",
            "Disponible : la page déclare le produit InStock",
            text_excerpt(visible, ["livraison", "retrait", "ajouter au panier"]),
        )

    # Pour limiter les faux positifs, certaines enseignes exigent plusieurs
    # indices positifs sur la même page.
    required_count = int(config.get("minimum_positive_matches", 1))
    positive_matches = [
        phrase for phrase in config.get("positive_phrases", [])
        if phrase.lower() in lower
    ]
    if len(positive_matches) >= required_count:
        detail = "Disponible : " + ", ".join(positive_matches[:3])
        return "available", detail, text_excerpt(visible, positive_matches)

    if restock:
        return "preorder", f"Information de réapprovisionnement : {restock}", text_excerpt(visible, [restock])

    return "unknown", "État non déterminé automatiquement", visible[:500]


async def relevant_text(page, config: dict[str, Any]) -> str:
    """
    Pour les pages de catégorie Boulanger/Darty, essaie d'isoler la carte du
    PortaSplit. Pour les pages produit, utilise simplement le corps de page.
    """
    patterns = config.get("product_text_patterns", [])
    for pattern in patterns:
        try:
            target = page.get_by_text(re.compile(pattern, re.IGNORECASE)).first
            await target.wait_for(state="attached", timeout=4_000)
            # Cherche un conteneur commercial proche.
            container = target.locator(
                "xpath=ancestor::*[self::article or self::li or "
                "@data-testid or contains(@class,'product') or contains(@class,'card')][1]"
            )
            if await container.count():
                candidate = normalize(await container.inner_text(timeout=5_000))
                if len(candidate) >= 30:
                    return candidate
        except Exception:
            pass
    return normalize(await page.locator("body").inner_text(timeout=10_000))


async def check_store(browser, config: dict[str, Any]) -> CheckResult:
    store = config["name"]
    url = config["url"]
    checked_at = datetime.now(timezone.utc).isoformat()

    context = await browser.new_context(
        locale="fr-FR",
        timezone_id="Europe/Paris",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 1100},
    )
    page = await context.new_page()

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=int(os.getenv("PAGE_TIMEOUT_MS", "45000")),
        )
        # Laisse le contenu JavaScript se stabiliser sans attendre
        # indéfiniment les traqueurs publicitaires.
        await page.wait_for_timeout(int(os.getenv("RENDER_WAIT_MS", "4500")))

        text = await relevant_text(page, config)
        page_html = await page.content()

        # Vérifie que la page concerne encore le bon produit.
        identity_terms = config.get(
            "identity_terms",
            ["PortaSplit", PRODUCT_MODEL, PRODUCT_EAN],
        )
        if not any(term.lower() in (text + " " + page_html).lower() for term in identity_terms):
            status, detail, excerpt = (
                "unknown",
                "Le produit exact n'a pas été retrouvé sur cette page",
                text[:500],
            )
        else:
            status, detail, excerpt = classify(text, page_html, config)

        signature_source = f"{status}|{detail}|{excerpt[:700]}"
        signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:20]
        return CheckResult(
            store=store,
            url=url,
            status=status,
            detail=detail,
            excerpt=excerpt[:900],
            signature=signature,
            checked_at=checked_at,
        )

    except PlaywrightTimeoutError as exc:
        return CheckResult(
            store=store,
            url=url,
            status="error",
            detail="Délai dépassé pendant le chargement",
            excerpt="",
            signature=hashlib.sha256(f"{store}|timeout".encode()).hexdigest()[:20],
            checked_at=checked_at,
            error=str(exc),
        )
    except Exception as exc:
        return CheckResult(
            store=store,
            url=url,
            status="error",
            detail="Erreur de vérification",
            excerpt="",
            signature=hashlib.sha256(f"{store}|{type(exc).__name__}".encode()).hexdigest()[:20],
            checked_at=checked_at,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        await context.close()


def smtp_settings() -> dict[str, Any]:
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "ALERT_TO"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            "Variables SMTP manquantes : " + ", ".join(missing)
            + ". Consultez README.md."
        )
    return {
        "host": os.environ["SMTP_HOST"],
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.environ["SMTP_USER"],
        "password": os.environ["SMTP_PASSWORD"],
        "to": os.environ["ALERT_TO"],
        "sender": os.getenv("ALERT_FROM", os.environ["SMTP_USER"]),
        "use_ssl": os.getenv("SMTP_SSL", "false").lower() == "true",
    }


def send_email(subject: str, results: list[CheckResult], test: bool = False) -> None:
    settings = smtp_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings["sender"]
    msg["To"] = settings["to"]

    if test:
        text_body = (
            "Ceci est un e-mail de test du moniteur Midea PortaSplit.\n\n"
            "La configuration SMTP fonctionne."
        )
        html_body = (
            "<h2>Test du moniteur Midea PortaSplit</h2>"
            "<p>La configuration SMTP fonctionne.</p>"
        )
    else:
        text_parts = [
            "Une disponibilité ou une information de réapprovisionnement "
            "a changé pour le Midea PortaSplit 12 000 BTU.",
            "",
        ]
        cards = []
        for result in results:
            text_parts.extend([
                f"{result.store}: {result.detail}",
                result.url,
                result.excerpt,
                "",
            ])
            cards.append(
                f"""
                <div style="border:1px solid #ddd;border-radius:8px;padding:16px;margin:12px 0">
                  <h3 style="margin-top:0">{html.escape(result.store)}</h3>
                  <p><strong>{html.escape(result.detail)}</strong></p>
                  <p>{html.escape(result.excerpt)}</p>
                  <p><a href="{html.escape(result.url)}">Ouvrir la page produit</a></p>
                </div>
                """
            )
        text_body = "\n".join(text_parts)
        html_body = (
            "<h2>Alerte Midea PortaSplit</h2>"
            "<p>Un changement potentiellement utile a été détecté.</p>"
            + "".join(cards)
            + "<p>Vérifiez rapidement la page avant de commander : "
              "les stocks peuvent disparaître en quelques minutes.</p>"
        )

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if settings["use_ssl"]:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings["host"], settings["port"], context=context, timeout=30
        ) as server:
            server.login(settings["user"], settings["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(settings["user"], settings["password"])
            server.send_message(msg)


def should_alert(
    previous: dict[str, Any] | None,
    current: CheckResult,
    bootstrap_silent: bool,
) -> bool:
    if previous is None:
        return not bootstrap_silent and current.status in {"available", "preorder"}

    previous_status = previous.get("status")
    previous_signature = previous.get("signature")

    # Une disponibilité réelle mérite toujours une alerte lors du changement.
    if current.status == "available":
        return previous_status != "available" or previous_signature != current.signature

    # Une nouvelle date de réapprovisionnement ou une précommande modifiée.
    if current.status == "preorder":
        return previous_status != "preorder" or previous_signature != current.signature

    return False


async def run_checks() -> tuple[list[CheckResult], dict[str, Any], list[CheckResult]]:
    configs = load_json(CONFIG_PATH, [])
    if not isinstance(configs, list) or not configs:
        raise RuntimeError("sites.json est vide ou invalide.")

    previous_state = load_json(STATE_PATH, {})
    bootstrap_silent = os.getenv("BOOTSTRAP_SILENT", "true").lower() == "true"
    alerts: list[CheckResult] = []
    results: list[CheckResult] = []
    new_state: dict[str, Any] = dict(previous_state)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for config in configs:
                if not config.get("enabled", True):
                    continue
                result = await check_store(browser, config)
                results.append(result)

                previous = previous_state.get(result.store)
                if should_alert(previous, result, bootstrap_silent):
                    alerts.append(result)

                # Les erreurs temporaires ne remplacent pas un état fiable antérieur.
                if result.status != "error":
                    new_state[result.store] = {
                        "status": result.status,
                        "detail": result.detail,
                        "excerpt": result.excerpt,
                        "signature": result.signature,
                        "url": result.url,
                    }
        finally:
            await browser.close()

    return results, new_state, alerts


def print_summary(results: list[CheckResult]) -> None:
    print(f"Surveillance {PRODUCT_MODEL} / EAN {PRODUCT_EAN}")
    for result in results:
        print(f"- {result.store}: {result.status} — {result.detail}")
        if result.error:
            print(f"  erreur: {result.error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Envoie un e-mail de test sans vérifier les magasins.",
    )
    parser.add_argument(
        "--force-alert",
        action="store_true",
        help="Envoie le résumé courant même sans changement.",
    )
    args = parser.parse_args()

    if args.test_email:
        send_email("Test — surveillance Midea PortaSplit", [], test=True)
        print("E-mail de test envoyé.")
        return 0

    try:
        results, new_state, alerts = asyncio.run(run_checks())
        print_summary(results)
        save_json_if_changed(STATE_PATH, new_state)

        if args.force_alert:
            alerts = [
                result for result in results
                if result.status in {"available", "preorder", "unknown"}
            ]

        if alerts:
            stores = ", ".join(result.store for result in alerts)
            send_email(f"Alerte stock Midea PortaSplit — {stores}", alerts)
            print(f"Alerte envoyée pour : {stores}")
        else:
            print("Aucune nouvelle disponibilité : aucun e-mail envoyé.")
        return 0
    except Exception as exc:
        print(f"ERREUR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
