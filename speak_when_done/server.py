"""
MCP Server for text-to-speech notifications.

Uses speak_when_done to speak notifications to the user when Claude
needs their attention after long-running tasks complete.
"""

import logging
import sys

from mcp.server.fastmcp import FastMCP

from . import speak as speak_fn
from . import list_voices as list_voices_fn

# Configure logging to stderr (never stdout - it corrupts JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create the MCP server
mcp = FastMCP("speak_when_done")


@mcp.tool()
def speak(message: str, voice: str = "alba") -> dict:
    """
    Speak a message aloud to notify the user.

    Use this tool ONLY when you need to get the user's attention after
    a long-running task has completed. Do not use for routine responses.

    Good examples:
    - "Your build has completed successfully"
    - "The test suite finished with 3 failures"
    - "Deployment is complete"
    - "I found the bug you were looking for"

    Args:
        message: The message to speak aloud. Keep it brief and informative.
        voice: Voice to use for speech (default: alba). Can be a built-in
               voice name or path to an audio file for voice cloning.

    Returns:
        Dictionary with success status and details.
    """
    logger.info(f"Speaking message: {message[:50]}...")
    result = speak_fn(message, voice=voice, quiet=True)

    if result["success"]:
        logger.info("Message spoken successfully")
    else:
        logger.error(f"Speech failed: {result.get('error')}")

    return result


@mcp.tool()
def list_voices() -> dict:
    """
    List available voices for text-to-speech.

    Use this tool to discover what voices are available before using the speak tool.
    Returns a list of built-in voice names and information about custom voice cloning.

    Returns:
        Dictionary with:
        - builtin_voices: List of available voice names with descriptions
        - default_voice: The default voice used if none specified
        - custom_voice_hint: Instructions for using custom voices
    """
    logger.info("Listing available voices")
    return list_voices_fn()


def run_server():
    """Run the MCP server."""
    logger.info("Starting speak_when_done MCP server")
    mcp.run()


if __name__ == "__main__":
    run_server()
