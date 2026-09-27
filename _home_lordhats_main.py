"""
NIM Agent — Entry Point
Starts the Discord bot and agent core.
"""

import asyncio
import logging
import sys
from agent.config import Config
from agent.core import Agent
from discord_bot.bot import NIMBot


def setup_logging(data_dir):
    """Configure logging to console + file."""
    log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    log_file = data_dir / "nim_agent.log"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_file), encoding="utf-8"),
    ]

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=handlers,
    )
    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)


async def main():
    # Load config
    config = Config()
    setup_logging(config.data_dir)
    logger = logging.getLogger("nim_agent.main")

    # Validate
    errors = config.validate()
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        print("\n❌ Configuration errors found:")
        for e in errors:
            print(f"   • {e}")
        print("\nPlease check your .env file. See .env.example for reference.")
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("NIM Agent starting up...")
    logger.info("  Model: %s", config.model_name)
    logger.info("  Data dir: %s", config.data_dir)
    logger.info("  Allowed users: %s", config.allowed_users)
    logger.info("=" * 50)

    # Initialize agent
    agent = Agent(config)
    await agent.start()

    # Initialize Discord bot
    bot = NIMBot(agent, config)
    await bot.start()

    # Start self-improvement loop
    await agent.start_improvement_loop()

    logger.info("🤖 NIM Agent is online and ready!")

    # Keep running until interrupted
    try:
        # Run forever
        stop_event = asyncio.Event()
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
    finally:
        await bot.stop()
        await agent.stop()
        logger.info("NIM Agent stopped. Goodbye! 👋")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
