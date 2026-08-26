from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

# Load ANTHROPIC_API_KEY from your .env file into this script
load_dotenv()

# Set up the connection to Claude
model = ChatAnthropic(model="claude-sonnet-4-6")

# Send one message and print what Claude says back
response = model.invoke("Say hello and confirm you're working.")
print(response.content)