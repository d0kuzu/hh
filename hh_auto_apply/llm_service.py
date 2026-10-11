import re

from openai import OpenAI

from .config import Config
from .models import Vacancy
from .prompt_templates import BANNED_PHRASES, SYSTEM_PROMPT, build_input


def validate_letter(letter: str) -> list[str]:
    errors = []
    if not letter.strip():
        return ["пустой ответ"]
    if len(letter.split()) > 100:
        errors.append("больше 100 слов")
    # Не разделяем дробные числа; сокращения лучше избегать в коротком письме.
    sentences = re.split(r"[.!?]+(?:[\s\"»]+|$)", letter.strip())
    if not 3 <= len([s for s in sentences if s.strip()]) <= 5:
        errors.append("нужно 3–5 предложений, включая приветствие")
    if any(p in letter.casefold() for p in BANNED_PHRASES):
        errors.append("обнаружены запрещённые штампы")
    if any(x in letter for x in ("```", "**", "{", "}", "[", "]")):
        errors.append("Markdown или незаполненный шаблон")
    if re.search(r"https?://|www\.", letter, re.I):
        errors.append("посторонняя ссылка")
    return errors


class LLMService:
    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url,
                             timeout=45.0, max_retries=2)

    def generate(self, profile: dict, vacancy: Vacancy) -> str:
        payload = build_input(profile, vacancy)
        instructions = SYSTEM_PROMPT
        for _ in range(3):
            if self.config.api_mode == "responses":
                response = self.client.responses.create(
                    model=self.config.model, instructions=instructions,
                    input=payload, max_output_tokens=1200, store=False,
                )
                letter = response.output_text.strip()
            else:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "system", "content": instructions},
                              {"role": "user", "content": payload}],
                    max_tokens=1200,
                )
                letter = (response.choices[0].message.content or "").strip()
            errors = validate_letter(letter)
            if not errors:
                return letter
            instructions = SYSTEM_PROMPT + "\nИсправь ошибки предыдущей попытки: " + "; ".join(errors)
        raise ValueError("LLM не прошла проверку стиля: " + "; ".join(errors))

    def close(self):
        self.client.close()

