"""Gemini Enterprise Agent Platform quickstart — test Google Gen AI SDK."""

import os
from google import genai

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")

client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="What is the capital of France?",
)
print("Model: gemini-2.5-flash")
print(f"Response: {response.text}")
