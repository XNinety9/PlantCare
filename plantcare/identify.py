import base64
import io
import json
import os
import shutil
import subprocess
from typing import TypeVar

from anthropic import Anthropic
from PIL import Image, ImageOps
from pydantic import BaseModel

from .models import Diagnosis, PlantIdentification

MODEL = os.environ.get("PLANTCARE_MODEL", "claude-haiku-5-5")
MAX_EDGE = 1568  # au-delà, Claude ne voit pas plus de détails et on paie juste plus cher
CLI_TIMEOUT_SECONDS = 180

SHEET_RULES = """- The sheet is displayed as badges and gauges: be concise, respect the maximum lengths, no filler.
- Give concrete values: sizes in cm, intervals in days or months, temperatures in °C.
- Winter watering is usually sparser than in the growing season; fertilize_interval_days is for spring and summer only.
- Write every text twice: in English (texts.en) and in French (texts.fr), with the same content.
- In French, address the reader informally with "tu", never "vous"."""

PROMPT = f"""You are a botanist. Identify the plant in this photo and write its care sheet for someone keeping it indoors.

- If the photo does not show a plant, set is_plant to false, and care and texts to null.
- Give the scientific name and your confidence between 0 and 1.
- If you hesitate between several species, lower the confidence and say so in one of the tips.
{SHEET_RULES}"""

NAME_PROMPT = """You are a botanist. Write the indoor care sheet of this plant: {name}.

- Put the most precise scientific name you know in scientific_name, and set is_plant to true.
- Set confidence to 1: the user named the plant themselves.
""" + SHEET_RULES

DIAGNOSIS_PROMPT = """You are a plant doctor. This photo shows a {name} ({scientific_name}) kept indoors. The owner is worried about it.

Its usual care: water every {watering} days in the growing season, light: {light}, humidity: {humidity}.

- Look for signs of trouble: discoloration, spots, wilting, pests, rot, sunburn, root issues.
- If the plant looks fine, say so with severity "healthy" and give simple maintenance actions.
- If the photo does not show a plant or a part of a plant, set shows_plant to false and texts to null.
- Be concise and concrete; respect the maximum lengths.
- Write every text twice: in English (texts.en) and in French (texts.fr), with the same content.
- In French, address the reader informally with "tu", never "vous"."""

client = Anthropic()

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class IdentificationError(Exception):
    """Le message est un code d'erreur, traduit par l'interface."""


def _prepare_image(data: bytes) -> str:
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    image = image.convert("RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return base64.standard_b64encode(buffer.getvalue()).decode("ascii")


def _image_block(data: bytes) -> dict:
    try:
        encoded = _prepare_image(data)
    except OSError as error:
        raise IdentificationError("unreadable_image") from error
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded}}


def identify(data: bytes) -> PlantIdentification:
    return _ask([_image_block(data), {"type": "text", "text": PROMPT}], PlantIdentification)


def describe(name: str) -> PlantIdentification:
    """Génère la fiche à partir d'un nom donné par l'utilisateur, sans photo."""
    return _ask([{"type": "text", "text": NAME_PROMPT.format(name=name)}], PlantIdentification)


def diagnose(data: bytes, *, name: str, scientific_name: str, watering: int, light: str, humidity: str) -> Diagnosis:
    prompt = DIAGNOSIS_PROMPT.format(
        name=name, scientific_name=scientific_name, watering=watering, light=light, humidity=humidity,
    )
    return _ask([_image_block(data), {"type": "text", "text": prompt}], Diagnosis)


def _ask(content: list[dict], output: type[ResponseT]) -> ResponseT:
    if shutil.which("claude"):
        return _ask_cli(content, output)
    return _ask_api(content, output)


def _ask_cli(content: list[dict], output: type[ResponseT]) -> ResponseT:
    message = {"type": "user", "message": {"role": "user", "content": content}}
    command = [
        "claude", "-p",
        "--model", MODEL,
        "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(output.model_json_schema()),
        # Sans ça, chaque appel embarque tout le contexte de Claude Code (outils, MCP, réglages)
        "--tools", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "user",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    try:
        process = subprocess.run(
            command,
            input=json.dumps(message) + "\n",
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise IdentificationError("cli_timeout") from error

    events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    result = next((event for event in events if event.get("type") == "result"), None)
    if process.returncode != 0 or result is None or result.get("is_error") or result.get("structured_output") is None:
        raise IdentificationError("cli_failed")
    return output.model_validate(result["structured_output"])


def _ask_api(content: list[dict], output: type[ResponseT]) -> ResponseT:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": content}],
        output_format=output,
    )
    if response.parsed_output is None:
        raise IdentificationError("no_result")
    return response.parsed_output
