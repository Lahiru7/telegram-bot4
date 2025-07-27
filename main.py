"""
Optimized Telegram Translation Bot with improved error handling, caching, concurrency, and filename selection.
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
from uuid import uuid4

import aiofiles
from deep_translator import GoogleTranslator
from flask import Flask
from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified, FloodWait
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    CallbackQuery
)


# Configuration
@dataclass
class Config:
    """Configuration class with validation"""
    API_ID: int = 28902210
    API_HASH: str = "0a3d884a11fa085cd872250a2140e344"
    BOT_TOKEN: str = "8146980022:AAEPi_HIbbaevGb9LhYelfY_kMFW6djCKXs"

    # Performance settings
    MAX_CONCURRENT_TRANSLATIONS: int = 8
    MAX_CONCURRENT_DOWNLOADS: int = 3
    BATCH_SIZE: int = 20
    PROGRESS_UPDATE_INTERVAL: int = 5  # seconds

    # Cache settings
    CACHE_SIZE: int = 1000
    CACHE_TTL_HOURS: int = 24

    # File settings
    MAX_FILE_SIZE_MB: int = 50
    ALLOWED_EXTENSIONS: List[str] = field(default_factory=lambda: ['.srt'])

    # User authorization
    ALLOWED_USERS: List[int] = field(default_factory=lambda: [7294451741, 1178035103, 1940436756])

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


# Enhanced caching system
@dataclass
class CacheEntry:
    """Cache entry with TTL support"""
    value: str
    timestamp: datetime
    access_count: int = 0


class SmartCache:
    """Thread-safe LRU cache with TTL support"""

    def __init__(self, max_size: int = 1000, ttl_hours: int = 24):
        self.max_size = max_size
        self.ttl = timedelta(hours=ttl_hours)
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[str]:
        """Get value from cache if exists and not expired"""
        async with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None

            # Check if expired
            if datetime.now() - entry.timestamp > self.ttl:
                del self._cache[key]
                return None

            entry.access_count += 1
            return entry.value

    async def set(self, key: str, value: str) -> None:
        """Set value in cache with LRU eviction"""
        async with self._lock:
            # Remove expired entries
            await self._cleanup_expired()

            # LRU eviction if at capacity
            if len(self._cache) >= self.max_size and key not in self._cache:
                # Remove least recently used
                lru_key = min(self._cache.keys(),
                              key=lambda k: (self._cache[k].access_count, self._cache[k].timestamp))
                del self._cache[lru_key]

            self._cache[key] = CacheEntry(value=value, timestamp=datetime.now())

    async def _cleanup_expired(self) -> None:
        """Remove expired entries"""
        now = datetime.now()
        expired_keys = [
            key for key, entry in self._cache.items()
            if now - entry.timestamp > self.ttl
        ]
        for key in expired_keys:
            del self._cache[key]

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "ttl_hours": self.ttl.total_seconds() / 3600
        }


class TranslationBot:
    """Main bot class with improved architecture"""

    def __init__(self, config: Config):
        self.config = config
        self.setup_logging()

        # Initialize components
        self.cache = SmartCache(config.CACHE_SIZE, config.CACHE_TTL_HOURS)
        self.callback_store: Dict[str, Tuple[str, str]] = {}
        self.active_translations: Dict[str, bool] = {}
        self.pending_filename: Dict[int, Tuple[str, str, str, str]] = {}  # user_id -> (file_id, original_name, key)
        self.waiting_for_filename: Dict[int, Tuple[str, str, str]] = {}  # user_id -> (file_id, original_name, key)

        # Semaphores for concurrency control
        self.translation_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_TRANSLATIONS)
        self.download_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)

        # Initialize Pyrogram client
        self.app = Client(
            "translation_bot",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN
        )

        self.setup_handlers()

    def setup_logging(self) -> None:
        """Configure logging"""
        logging.basicConfig(
            level=getattr(logging, self.config.LOG_LEVEL),
            format=self.config.LOG_FORMAT
        )
        self.logger = logging.getLogger(__name__)

    def setup_handlers(self) -> None:
        """Setup Pyrogram handlers"""
        self.app.on_message(filters.document & filters.user(self.config.ALLOWED_USERS))(
            self.handle_document
        )
        self.app.on_message(filters.text & filters.user(self.config.ALLOWED_USERS) & filters.private)(
            self.handle_filename_input
        )
        self.app.on_callback_query(filters.regex(r"^(filename|translate)\|"))(
            self.handle_callback
        )

    def is_valid_subtitle_file(self, filename: str, file_size: int) -> Tuple[bool, str]:
        """Validate subtitle file"""
        if not filename:
            return False, "No filename provided"

        # Check extension
        file_ext = Path(filename).suffix.lower()
        if file_ext not in self.config.ALLOWED_EXTENSIONS:
            return False, f"Only {', '.join(self.config.ALLOWED_EXTENSIONS)} files are supported"

        # Check file size
        max_size_bytes = self.config.MAX_FILE_SIZE_MB * 1024 * 1024
        if file_size > max_size_bytes:
            return False, f"File too large. Maximum size: {self.config.MAX_FILE_SIZE_MB}MB"

        return True, ""

    async def handle_document(self, client: Client, message: Message) -> Message | None:
        """Handle document uploads with validation"""
        try:
            doc = message.document
            if not doc:
                await message.reply_text("⚠️ No document found in message.")
                return

            # Validate file
            is_valid, error_msg = self.is_valid_subtitle_file(doc.file_name, doc.file_size)
            if not is_valid:
                await message.reply_text(f"⚠️ {error_msg}")
                return

            # Generate unique key and store file info
            key = uuid4().hex[:8]
            self.callback_store[key] = (doc.file_id, doc.file_name)

            # Send confirmation with filename selection options
            await message.reply_text(f"✅ File received: {doc.file_name}")

            # Create filename selection buttons
            buttons = [
                [InlineKeyboardButton("📁 Keep Original Filename", callback_data=f"filename|keep|{key}")],
                [InlineKeyboardButton("✏️ Use Custom Filename", callback_data=f"filename|custom|{key}")],
                [InlineKeyboardButton("🌐 Start Translation (Default)", callback_data=f"translate|{key}")]
            ]
            markup = InlineKeyboardMarkup(buttons)

            await message.reply_text(
                "Choose filename option for the translated file:\n\n"
                "📁 **Keep Original**: Uses the original filename with translation suffix\n"
                "✏️ **Custom**: You can specify a new filename\n"
                "🌐 **Default**: Uses default naming pattern\n\n"
                "Select an option below 👇",
                reply_markup=markup
            )

            self.logger.info(f"Document {doc.file_name} queued with key {key}")

        except Exception as e:
            self.logger.error(f"Error handling document: {e}")
            await message.reply_text("⚠️ An error occurred while processing your file.")
            return

    async def handle_filename_input(self, client: Client, message: Message) -> None:
        """Handle custom filename input from user"""
        user_id = message.from_user.id

        # Check if user is waiting to provide a filename
        if user_id not in self.waiting_for_filename:
            return  # Not waiting for filename, ignore

        try:
            # Get the stored file information
            file_id, original_name, key = self.waiting_for_filename[user_id]
            custom_filename = message.text.strip()

            # Validate custom filename
            if not custom_filename:
                await message.reply_text("⚠️ Please provide a valid filename.")
                return

            # Ensure it has .srt extension
            if not custom_filename.lower().endswith('.srt'):
                custom_filename += '.srt'

            # Clean up waiting state
            del self.waiting_for_filename[user_id]

            # Store the custom filename choice
            self.pending_filename[user_id] = (file_id, original_name, key, custom_filename)

            # Confirm and provide translation button
            await message.reply_text(
                f"✅ Custom filename set: `{custom_filename}`\n\n"
                "Click the button below to start translation 👇"
            )

            button = InlineKeyboardButton("🌐 Start Translation", callback_data=f"translate|{key}")
            markup = InlineKeyboardMarkup([[button]])
            await message.reply_text("Ready to translate!", reply_markup=markup)

        except Exception as e:
            self.logger.error(f"Error handling filename input: {e}")
            await message.reply_text("⚠️ An error occurred while processing your filename.")
            # Clean up on error
            self.waiting_for_filename.pop(user_id, None)

    async def handle_callback(self, client: Client, callback_query: CallbackQuery) -> None:
        """Handle all callback queries"""
        try:
            user_id = callback_query.from_user.id
            if user_id not in self.config.ALLOWED_USERS:
                return await callback_query.answer("⛔️ Unauthorized access", show_alert=True)

            callback_data = callback_query.data

            if callback_data.startswith("filename|"):
                await self.handle_filename_callback(client, callback_query)
            elif callback_data.startswith("translate|"):
                await self.handle_translation_callback(client, callback_query)

        except Exception as e:
            self.logger.error(f"Error in callback handler: {e}")
            try:
                await callback_query.answer("⚠️ An error occurred", show_alert=True)
            except:
                pass

    async def handle_filename_callback(self, client: Client, callback_query: CallbackQuery) -> None:
        """Handle filename selection callbacks"""
        try:
            user_id = callback_query.from_user.id
            _, action, key = callback_query.data.split("|")

            # Validate key
            entry = self.callback_store.get(key)
            if not entry:
                return await callback_query.answer(
                    "⚠️ Request expired. Please resend the file.",
                    show_alert=True
                )

            file_id, original_name = entry

            if action == "keep":
                # Keep original filename
                await callback_query.answer("📁 Original filename selected")
                self.pending_filename[user_id] = (file_id, original_name, key, "original")

                # Show translation button
                button = InlineKeyboardButton("🌐 Start Translation", callback_data=f"translate|{key}")
                markup = InlineKeyboardMarkup([[button]])
                await callback_query.message.edit_text(
                    f"✅ Selected: Keep original filename\n"
                    f"📁 Will use: `{Path(original_name).stem}Sinhala.Sub.@ADL_Drama{Path(original_name).suffix}`\n\n"
                    "Click below to start translation 👇",
                    reply_markup=markup
                )

            elif action == "custom":
                # Request custom filename
                await callback_query.answer("✏️ Custom filename selected")
                self.waiting_for_filename[user_id] = (file_id, original_name, key)

                await callback_query.message.edit_text(
                    "✏️ **Custom Filename Selected**\n\n"
                    "Please send your desired filename for the translated file.\n"
                    "📝 **Example:** `My Movie Sinhala Subtitles`\n"
                    "🔤 **Note:** `.srt` extension will be added automatically if not provided.\n\n"
                    "💬 Type your filename in the next message:"
                )

        except Exception as e:
            self.logger.error(f"Error handling filename callback: {e}")
            await callback_query.answer("⚠️ An error occurred", show_alert=True)

    async def handle_translation_callback(self, client: Client, callback_query: CallbackQuery) -> None:
        """Handle translation callback with comprehensive error handling"""
        try:
            user_id = callback_query.from_user.id
            if user_id not in self.config.ALLOWED_USERS:
                return await callback_query.answer("⛔️ Unauthorized access", show_alert=True)

            await callback_query.answer("🔄 Starting translation process...")

            # Extract key and validate
            _, key = callback_query.data.split("|", 1)
            if key in self.active_translations:
                return await callback_query.answer("⚠️ Translation already in progress", show_alert=True)

            # Get file info and custom filename if set
            entry = self.callback_store.get(key)
            if not entry:
                return await callback_query.answer(
                    "⚠️ Translation request expired. Please resend the file.",
                    show_alert=True
                )

            file_id, original_name = entry

            # Check for custom filename preference
            custom_filename = None
            if user_id in self.pending_filename:
                stored_file_id, stored_original, stored_key, filename_choice = self.pending_filename[user_id]
                if stored_key == key:
                    custom_filename = filename_choice

            self.active_translations[key] = True

            try:
                await self.process_translation(client, callback_query, file_id, original_name, key, custom_filename)
            finally:
                # Cleanup
                self.active_translations.pop(key, None)
                self.callback_store.pop(key, None)
                self.pending_filename.pop(user_id, None)
                self.waiting_for_filename.pop(user_id, None)

        except Exception as e:
            self.logger.error(f"Error in translation callback: {e}")
            try:
                await callback_query.message.reply_text("⚠️ Translation failed due to an unexpected error.")
            except:
                pass

    async def process_translation(
            self,
            client: Client,
            callback_query: CallbackQuery,
            file_id: str,
            original_name: str,
            key: str,
            custom_filename: Optional[str] = None
    ) -> None:
        """Process the complete translation workflow"""
        status_msg = None
        temp_file_path = None
        output_path = None
        try:
            # Send initial progress message
            status_msg = await client.send_message(
                callback_query.message.chat.id,
                "🔄 Initializing translation... 0%"
            )

            # Download file with semaphore control
            async with self.download_semaphore:
                await self.update_progress(status_msg, "📥 Downloading file...", 5)
                temp_file_path = await client.download_media(file_id, file_name=original_name)

            # Parse subtitle file
            await self.update_progress(status_msg, "📖 Parsing subtitle file...", 10)
            subtitle_blocks = await self.parse_subtitle_file(temp_file_path)

            if not subtitle_blocks:
                await status_msg.edit_text("⚠️ No subtitle content found in file.")
                return

            total_blocks = len(subtitle_blocks)
            self.logger.info(f"Parsed {total_blocks} subtitle blocks from {original_name}")

            # Translate with progress tracking
            translated_blocks = await self.translate_subtitle_blocks(
                subtitle_blocks, status_msg, total_blocks
            )

            # Generate output file
            await self.update_progress(status_msg, "📝 Generating translated file...", 95)
            output_path = await self.create_output_file(translated_blocks, original_name)

            # Determine final filename
            if custom_filename == "original":
                # Use original filename with suffix
                new_filename = f"{Path(original_name).stem}Sinhala.Sub.@ADL_Drama{Path(original_name).suffix}"
            elif custom_filename and custom_filename != "original":
                # Use custom filename
                new_filename = custom_filename
            else:
                # Use default pattern
                new_filename = f"{Path(original_name).stem}Sinhala.Sub.@ADL_Drama{Path(original_name).suffix}"

            # Send translated file
            await client.send_document(
                callback_query.message.chat.id,
                output_path,
                file_name=new_filename
            )

            await status_msg.edit_text(
                f"✅ Translation completed successfully!\n"
                f"📁 Filename: `{new_filename}`"
            )
            self.logger.info(f"Translation completed for {original_name} -> {new_filename}")

        except Exception as e:
            self.logger.error(f"Translation process failed: {e}")
            if status_msg:
                try:
                    await status_msg.edit_text(f"❌ Translation failed: {str(e)[:100]}...")
                except:
                    pass
            raise
        finally:
            # Cleanup temporary files
            for path in [temp_file_path, output_path if 'output_path' in locals() else None]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception as e:
                        self.logger.warning(f"Failed to remove temp file {path}: {e}")

    async def parse_subtitle_file(self, file_path: str) -> List[List[str]]:
        """Parse SRT file into subtitle blocks"""
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read()

            lines = content.splitlines()
            blocks = []
            current_block = []

            for line in lines:
                if line.strip():
                    current_block.append(line)
                else:
                    if current_block:
                        blocks.append(current_block)
                        current_block = []

            # Add final block if exists
            if current_block:
                blocks.append(current_block)

            return blocks

        except Exception as e:
            self.logger.error(f"Error parsing subtitle file: {e}")
            raise

    async def translate_subtitle_blocks(
            self,
            blocks: List[List[str]],
            status_msg: Message,
            total_blocks: int
    ) -> List[List[str]]:
        """Translate subtitle blocks with concurrency and caching"""

        async def translate_single_block(block_idx: int, block: List[str]) -> Tuple[int, List[str]]:
            """Translate a single subtitle block"""
            try:
                # Preserve index and timestamp (first 2 lines)
                if len(block) < 3:
                    return block_idx, block

                header = block[:2]
                text_lines = block[2:]
                text_to_translate = '\n'.join(text_lines)

                # Create cache key
                cache_key = hashlib.md5(text_to_translate.encode()).hexdigest()

                # Check cache first
                cached_translation = await self.cache.get(cache_key)
                if cached_translation:
                    translated_lines = cached_translation.split('\n')
                    return block_idx, header + translated_lines

                # Translate with semaphore control
                async with self.translation_semaphore:
                    translated_text = await asyncio.to_thread(
                        self._safe_translate, text_to_translate
                    )

                # Cache the result
                await self.cache.set(cache_key, translated_text)

                translated_lines = translated_text.split('\n')
                return block_idx, header + translated_lines

            except Exception as e:
                self.logger.warning(f"Error translating block {block_idx}: {e}")
                return block_idx, block  # Return original on error

        # Create translation tasks
        tasks = [
            asyncio.create_task(translate_single_block(i, block))
            for i, block in enumerate(blocks)
        ]

        # Progress tracking
        progress_task = asyncio.create_task(
            self._track_translation_progress(tasks, status_msg, total_blocks)
        )

        # Wait for all translations
        results = await asyncio.gather(*tasks, return_exceptions=True)
        progress_task.cancel()

        # Process results and sort by original order
        translated_blocks = []
        for result in results:
            if isinstance(result, Exception):
                self.logger.error(f"Translation task failed: {result}")
                continue
            translated_blocks.append(result)

        # Sort by block index to maintain order
        translated_blocks.sort(key=lambda x: x[0])

        return [block for _, block in translated_blocks]

    def _safe_translate(self, text: str) -> str:
        """Safe translation with retry logic"""
        max_retries = 3
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                translator = GoogleTranslator(source='auto', target='si')
                return translator.translate(text)
            except Exception as e:
                if attempt == max_retries - 1:
                    self.logger.error(f"Translation failed after {max_retries} attempts: {e}")
                    return text  # Return original text on final failure

                self.logger.warning(f"Translation attempt {attempt + 1} failed: {e}, retrying...")
                time.sleep(retry_delay * (attempt + 1))

        return text

    async def _track_translation_progress(
            self,
            tasks: List[asyncio.Task],
            status_msg: Message,
            total_blocks: int
    ) -> None:
        """Track and update translation progress"""
        last_update = 0

        while not all(task.done() for task in tasks):
            try:
                completed = sum(1 for task in tasks if task.done())
                progress = int((completed / total_blocks) * 85) + 15  # 15-100% range

                current_time = time.time()
                if current_time - last_update >= self.config.PROGRESS_UPDATE_INTERVAL:
                    await self.update_progress(
                        status_msg,
                        f"🔄 Translating subtitles... ({completed}/{total_blocks})",
                        progress
                    )
                    last_update = current_time

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning(f"Progress tracking error: {e}")

    async def update_progress(self, status_msg: Message, text: str, percentage: int) -> None:
        """Update progress message with rate limiting"""
        try:
            progress_bar = "█" * (percentage // 5) + "░" * (20 - percentage // 5)
            full_text = f"{text}\n[{progress_bar}] {percentage}%"
            await status_msg.edit_text(full_text)
        except MessageNotModified:
            pass  # Ignore if message content is the same
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            self.logger.warning(f"Failed to update progress: {e}")

    async def create_output_file(self, translated_blocks: List[List[str]], original_name: str) -> str:
        """Create the translated subtitle file"""
        output_lines = []

        for block in translated_blocks:
            output_lines.extend(block)
            output_lines.append("")  # Empty line between blocks

        # Create temporary output file
        output_path = tempfile.NamedTemporaryFile(
            mode='w',
            delete=False,
            suffix='.srt',
            encoding='utf-8'
        ).name

        async with aiofiles.open(output_path, 'w', encoding='utf-8') as f:
            await f.write('\n'.join(output_lines))

        return output_path

    def run(self) -> None:
        """Start the bot"""
        self.logger.info("Starting Translation Bot...")
        self.app.run()


# Flask keepalive server
class KeepAliveServer:
    """Simple Flask server for health checks"""

    def __init__(self, port: int = 8080):
        self.app = Flask(__name__)
        self.port = port
        self.setup_routes()

    def setup_routes(self) -> None:
        @self.app.route("/")
        def health_check():
            return {
                "status": "healthy",
                "timestamp": datetime.now().isoformat(),
                "service": "telegram-translation-bot"
            }

        @self.app.route("/stats")
        def stats():
            # Return basic stats if needed
            return {"cache_stats": bot.cache.get_stats() if 'bot' in globals() else {}}

    def run(self) -> None:
        """Run the Flask server"""
        self.app.run(host="0.0.0.0", port=self.port, debug=False)


def main():
    """Main entry point"""
    config = Config()

    global bot
    bot = TranslationBot(config)

    # Start keepalive server in background
    keepalive = KeepAliveServer()
    flask_thread = threading.Thread(target=keepalive.run, daemon=True)
    flask_thread.start()

    # Start the bot
    bot.run()


if __name__ == "__main__":
    main()