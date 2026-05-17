import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

SUPPORTED_MODELS = {
    "claude": "claude-sonnet-4-6",
    "gpt4": "gpt-4o",
    "gemini": "gemini-1.5-pro"
}

DEFAULT_MODEL = "claude"

def get_completion(prompt, model_key=DEFAULT_MODEL, max_tokens=1000, system=None):
    if model_key == "claude":
        return _call_claude(prompt, max_tokens, system)
    elif model_key == "gpt4":
        return _call_openai(prompt, max_tokens, system)
    elif model_key == "gemini":
        return _call_gemini(prompt, max_tokens, system)
    else:
        raise ValueError(f"Unsupported model: {model_key}. Choose from {list(SUPPORTED_MODELS.keys())}")

def _call_claude(prompt, max_tokens, system):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    messages = [{"role": "user", "content": prompt}]
    kwargs = {
        "model": SUPPORTED_MODELS["claude"],
        "max_tokens": max_tokens,
        "messages": messages
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    return response.content[0].text

def _call_openai(prompt, max_tokens, system):
    raise NotImplementedError("GPT-4 support coming soon")

def _call_gemini(prompt, max_tokens, system):
    raise NotImplementedError("Gemini support coming soon")

if __name__ == "__main__":
    response = get_completion("Say hello and tell me you are ready to help with a fantasy football draft.")
    print(response)