import asyncio
import os

from openai import AsyncAzureOpenAI

# Load your configurations into environment variables (or parse them directly)
os.environ["MODEL_NAME"] = "azure/gpt-4.1"
os.environ["AZURE_EMBEDDING_API_BASE"] = "https://xcpai-agents.services.ai.azure.com"
os.environ["AZURE_API_BASE"] = "https://gto-salestech-ai-service.openai.azure.com"
os.environ["AZURE_EMBEDDING_VERSION"] = "2023-05-15"
os.environ["AZURE_API_VERSION"] = "2025-01-01-preview"
os.environ["AZURE_OPENAI_DEPLOYMENT"] = "gpt-4.1"
os.environ["AZURE_OPENAI_ENDPOINT"] = "https://gto-salestech-ai-service.openai.azure.com/"
os.environ[
    "IAP_AZURE_OPENAI_API_KEY"] = "no key"
os.environ[
    "AZURE_CLIENT_ID"] = "no client id"


class AzureAIDriver:
    def __init__(self):
        # Primary Chat/Completions client using the endpoint resource
        self.chat_client = AsyncAzureOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_API_VERSION"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
        )

        # Secondary/Embedding client configuration (if hitting the base service directly)
        self.embedding_client = AsyncAzureOpenAI(
            api_key=os.getenv("IAP_AZURE_EMBEDDING_API_KEY"),
            api_version=os.getenv("AZURE_EMBEDDING_VERSION"),
            azure_endpoint=os.getenv("AZURE_EMBEDDING_API_BASE")
        )

        self.model_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")

    async def generate_chat(self, prompt: str, system_message: str = "You are a helpful AI assistant."):
        """Asynchronously generates a chat completion response."""
        try:
            response = await self.chat_client.chat.completions.create(
                model=self.model_deployment,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt}
                ]
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Error during chat completion: {e}")
            raise

    async def generate_embeddings(self, input_text: str, embedding_deployment: str = "text-embedding-ada-002"):
        """Asynchronously generates embeddings using the embedding endpoint configuration."""
        try:
            response = await self.embedding_client.embeddings.create(
                model=embedding_deployment,
                input=input_text
            )
            return response.data[0].embedding
        except Exception as e:
            print(f"Error during embedding generation: {e}")
            raise


async def main():
    # Initialize the driver
    driver = AzureAIDriver()
    print("Testing Async Chat Completion...")
    answer = await driver.generate_chat("Hello! Give me a quick status update.")
    print(f"Response:\n{answer}\n")

    answer = await driver.generate_embeddings("Hello! Give me a quick status update.")
    print(f"Response:\n{answer}\n")

if __name__ == "__main__":
    asyncio.run(main())
