import anthropic
import os
from dotenv import load_dotenv
from config import DEV_MODE

load_dotenv()

SUPPORTED_MODELS = {
    "claude":       "claude-sonnet-4-6",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "claude-opus":  "claude-opus-4-7",
    "gpt4":         "gpt-4o",
    "gemini":       "gemini-1.5-pro"
}

MODEL_COSTS = {
    "claude":       (3.00, 15.00),
    "claude-haiku": (1.00, 5.00),
    "claude-opus":  (5.00, 25.00),
}

DEFAULT_MODEL = "claude-haiku"

CLAUDE_MODELS = {"claude", "claude-haiku", "claude-opus"}

def get_completion(prompt, model_key=DEFAULT_MODEL, max_tokens=1000, system=None):
    if model_key in CLAUDE_MODELS:
        return _call_claude(prompt, model_key, max_tokens, system)
    elif model_key == "gpt4":
        return _call_openai(prompt, max_tokens, system)
    elif model_key == "gemini":
        return _call_gemini(prompt, max_tokens, system)
    else:
        raise ValueError(f"Unsupported model: {model_key}. Choose from {list(SUPPORTED_MODELS.keys())}")

def _call_claude(prompt, model_key, max_tokens, system):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    kwargs = {
        "model": SUPPORTED_MODELS[model_key],
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)

    if DEV_MODE:
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        input_rate, output_rate = MODEL_COSTS[model_key]
        cost = (input_tokens / 1_000_000 * input_rate) + (output_tokens / 1_000_000 * output_rate)
        print(f"[{SUPPORTED_MODELS[model_key]}] input:{input_tokens} output:{output_tokens} cost:${cost:.4f}")

    return response.content[0].text

def _call_openai(prompt, max_tokens, system):
    raise NotImplementedError("GPT-4 support coming soon")

def _call_gemini(prompt, max_tokens, system):
    raise NotImplementedError("Gemini support coming soon")

if __name__ == "__main__":
    response = get_completion("Say hello and tell me you are ready to help with a fantasy football draft.")
    print(response)