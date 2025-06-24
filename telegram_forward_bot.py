import asyncio
import logging
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
from telegram import Update, Message, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from telegram.constants import ParseMode
import sqlite3
from pathlib import Path
import html
import re
from collections import defaultdict

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def escape_markdown_v2(text: str) -> str:
    """转义 MarkdownV2 特殊字符"""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

class MediaGroupHandler:
    """媒体组处理器"""
    def __init__(self):
        self.media_groups: Dict[str, List[Message]] = defaultdict(list)
        self.group_timers: Dict[str, asyncio.Task] = {}
        self.timeout_seconds = 3  # 等待媒体组完成的超时时间
    
    async def add_message(self, message: Message, forward_callback):
        """添加消息到媒体组"""
        if not message.media_group_id:
            # 不是媒体组消息，直接转发
            await forward_callback([message])
            return
        
        group_id = message.media_group_id
        self.media_groups[group_id].append(message)
        
        # 如果已经有定时器，取消它
        if group_id in self.group_timers:
            self.group_timers[group_id].cancel()
        
        # 设置新的定时器
        self.group_timers[group_id] = asyncio.create_task(
            self._process_group_after_timeout(group_id, forward_callback)
        )
    
    async def _process_group_after_timeout(self, group_id: str, forward_callback):
        """超时后处理媒体组"""
        await asyncio.sleep(self.timeout_seconds)
        
        if group_id in self.media_groups:
            messages = self.media_groups[group_id]
            # 按消息ID排序确保顺序正确
            messages.sort(key=lambda m: m.message_id)
            await forward_callback(messages)
            
            # 清理
            del self.media_groups[group_id]
            if group_id in self.group_timers:
                del self.group_timers[group_id]

class TelegramForwardBot:
    def __init__(self, token: str):
        self.token = token
        self.application = Application.builder().token(token).build()
        self.db_path = "forward_bot.db"
        self.config_file = "bot_config.json"
        
        # 媒体组处理器
        self.media_group_handler = MediaGroupHandler()
        
        # 初始化数据库
        self.init_database()
        
        # 加载配置
        self.config = self.load_config()
        
        # 统计数据
        self.stats = {
            'messages_received': 0,
            'messages_forwarded': 0,
            'failed_forwards': 0,
            'media_groups_forwarded': 0,
            'start_time': datetime.now()
        }
        
        # 注册处理器
        self.register_handlers()
    
    def init_database(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 创建源频道/群组表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS source_channels (
                id INTEGER PRIMARY KEY,
                title TEXT,
                type TEXT,
                added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active BOOLEAN DEFAULT TRUE
            )
        ''')
        
        # 创建目标频道/群组表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS target_channels (
                id INTEGER PRIMARY KEY,
                title TEXT,
                type TEXT,
                added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active BOOLEAN DEFAULT TRUE
            )
        ''')
        
        # 创建转发日志表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS forward_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER,
                target_chat_id INTEGER,
                original_message_id INTEGER,
                forwarded_message_id INTEGER,
                content_type TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success BOOLEAN,
                error_message TEXT
            )
        ''')
        
        # 检查并添加新列
        cursor.execute("PRAGMA table_info(forward_logs)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'media_group_id' not in columns:
            cursor.execute('ALTER TABLE forward_logs ADD COLUMN media_group_id TEXT')
            logger.info("添加了 media_group_id 列")
        
        if 'is_media_group' not in columns:
            cursor.execute('ALTER TABLE forward_logs ADD COLUMN is_media_group BOOLEAN DEFAULT FALSE')
            logger.info("添加了 is_media_group 列")
        
        # 创建管理员表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def load_config(self) -> dict:
        """加载配置文件"""
        default_config = {
            "admins": [],  # 管理员用户ID列表
            "source_channels": [],  # 源频道/群组ID列表
            "target_channels": [],  # 目标频道/群组ID列表
            "forward_settings": {
                "preserve_sender": True,  # 保留发送者信息
                "add_source_info": True,  # 添加来源信息
                "filter_content_types": [],  # 过滤的内容类型
                "keyword_filter": [],  # 关键词过滤
                "delay_seconds": 0,  # 转发延迟(秒)
                "batch_forward": False,  # 批量转发模式
                "max_forwards_per_minute": 60,  # 每分钟最大转发数
                "media_group_timeout": 3  # 媒体组超时时间(秒)
            },
            "notification_settings": {
                "notify_admin_on_error": True,
                "daily_report": True,
                "report_channel": None
            }
        }
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    # 合并默认配置
                    for key, value in default_config.items():
                        if key not in config:
                            config[key] = value
                        elif isinstance(value, dict):
                            for sub_key, sub_value in value.items():
                                if sub_key not in config[key]:
                                    config[key][sub_key] = sub_value
                    return config
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")
                return default_config
        else:
            self.save_config(default_config)
            return default_config
    
    def save_config(self, config: dict = None):
        """保存配置文件"""
        if config is None:
            config = self.config
        
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
    
    def register_handlers(self):
        """注册消息处理器"""
        # 管理员命令
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        
        # 配置命令
        self.application.add_handler(CommandHandler("add_source", self.add_source_command))
        self.application.add_handler(CommandHandler("add_target", self.add_target_command))
        self.application.add_handler(CommandHandler("remove_source", self.remove_source_command))
        self.application.add_handler(CommandHandler("remove_target", self.remove_target_command))
        self.application.add_handler(CommandHandler("list_sources", self.list_sources_command))
        self.application.add_handler(CommandHandler("list_targets", self.list_targets_command))
        
        # 管理员管理
        self.application.add_handler(CommandHandler("add_admin", self.add_admin_command))
        self.application.add_handler(CommandHandler("list_admins", self.list_admins_command))
        
        # 设置命令
        self.application.add_handler(CommandHandler("settings", self.settings_command))
        self.application.add_handler(CommandHandler("set_delay", self.set_delay_command))
        self.application.add_handler(CommandHandler("toggle_source_info", self.toggle_source_info_command))
        
        # 消息转发处理器
        self.application.add_handler(MessageHandler(
            filters.ALL & (~filters.COMMAND), 
            self.handle_message
        ))
    
    async def is_admin(self, user_id: int) -> bool:
        """检查用户是否为管理员"""
        return user_id in self.config.get("admins", [])
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """开始命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            await update.message.reply_text("❌ 您没有权限使用此机器人")
            return
        
        welcome_text = """🤖 *Telegram 转发机器人已启动*

*主要功能:*
📢 自动转发指定频道/群组的消息到多个目标
📊 详细的转发统计和日志
⚙️ 灵活的配置选项
🔒 管理员权限控制
🖼️ 支持媒体组完整转发

*快速开始:*
1\\. `/add_source <频道ID>` \\- 添加源频道
2\\. `/add_target <频道ID>` \\- 添加目标频道
3\\. `/status` \\- 查看当前状态

输入 `/help` 查看所有命令"""
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN_V2)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """帮助命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        help_text = """📖 *命令列表*

*基础命令:*
• `/start` \\- 启动机器人
• `/help` \\- 显示帮助
• `/status` \\- 查看机器人状态
• `/stats` \\- 查看转发统计

*配置命令:*
• `/add_source <ID>` \\- 添加源频道/群组
• `/add_target <ID>` \\- 添加目标频道/群组
• `/remove_source <ID>` \\- 移除源频道/群组
• `/remove_target <ID>` \\- 移除目标频道/群组
• `/list_sources` \\- 列出所有源频道
• `/list_targets` \\- 列出所有目标频道

*管理命令:*
• `/add_admin <用户ID>` \\- 添加管理员
• `/list_admins` \\- 列出所有管理员

*设置命令:*
• `/settings` \\- 查看当前设置
• `/set_delay <秒数>` \\- 设置转发延迟
• `/toggle_source_info` \\- 切换来源信息显示

*获取频道ID方法:*
1\\. 将机器人添加到频道/群组
2\\. 发送任意消息
3\\. 查看日志获取ID

*媒体组支持:*
• 自动识别和完整转发多张图片
• 支持图片\\+视频混合媒体组
• 保持原始顺序和分组"""
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """状态命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        uptime = datetime.now() - self.stats['start_time']
        
        # 转义特殊字符
        uptime_str = escape_markdown_v2(str(uptime).split('.')[0])
        
        status_text = f"""📊 *机器人状态*

🕐 *运行时间:* {uptime_str}
📥 *接收消息:* {self.stats['messages_received']}
📤 *转发成功:* {self.stats['messages_forwarded']}
🖼️ *媒体组转发:* {self.stats['media_groups_forwarded']}
❌ *转发失败:* {self.stats['failed_forwards']}

📢 *源频道数量:* {len(self.config['source_channels'])}
🎯 *目标频道数量:* {len(self.config['target_channels'])}
👥 *管理员数量:* {len(self.config['admins'])}

⚙️ *当前设置:*
• 转发延迟: {self.config['forward_settings']['delay_seconds']}秒
• 显示来源: {'✅' if self.config['forward_settings']['add_source_info'] else '❌'}
• 保留发送者: {'✅' if self.config['forward_settings']['preserve_sender'] else '❌'}
• 媒体组超时: {self.config['forward_settings']['media_group_timeout']}秒"""
        await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN_V2)
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """统计命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        # 从数据库获取详细统计
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 今日转发统计
        cursor.execute('''
            SELECT COUNT(*), content_type, COALESCE(is_media_group, 0) as is_media_group
            FROM forward_logs 
            WHERE DATE(timestamp) = DATE('now')
            GROUP BY content_type, is_media_group
        ''')
        today_stats = cursor.fetchall()
        
        # 成功率统计
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success
            FROM forward_logs 
            WHERE DATE(timestamp) = DATE('now')
        ''')
        success_stats = cursor.fetchone()
        
        conn.close()
        
        stats_text = "📈 *详细统计*\n\n"
        
        if today_stats:
            stats_text += "📅 *今日转发统计:*\n"
            for count, content_type, is_media_group in today_stats:
                content_type_safe = escape_markdown_v2(content_type or '未知')
                group_indicator = " \\(媒体组\\)" if is_media_group else ""
                stats_text += f"• {content_type_safe}{group_indicator}: {count}条\n"
        
        if success_stats and success_stats[0] > 0:
            success_rate = (success_stats[1] / success_stats[0]) * 100
            stats_text += f"\n✅ *今日成功率:* {success_rate:.1f}%"
        
        await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN_V2)
    
    async def add_source_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """添加源频道命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ 请提供频道ID\n用法: `/add_source -1001234567890`")
            return
        
        try:
            channel_id = int(context.args[0])
            if channel_id not in self.config['source_channels']:
                self.config['source_channels'].append(channel_id)
                self.save_config()
                await update.message.reply_text(f"✅ 已添加源频道: `{channel_id}`", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text("❌ 该频道已存在于源列表中")
        except ValueError:
            await update.message.reply_text("❌ 无效的频道ID")
    
    async def add_target_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """添加目标频道命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ 请提供频道ID\n用法: `/add_target -1001234567890`")
            return
        
        try:
            channel_id = int(context.args[0])
            if channel_id not in self.config['target_channels']:
                self.config['target_channels'].append(channel_id)
                self.save_config()
                await update.message.reply_text(f"✅ 已添加目标频道: `{channel_id}`", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text("❌ 该频道已存在于目标列表中")
        except ValueError:
            await update.message.reply_text("❌ 无效的频道ID")
    
    async def remove_source_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """移除源频道命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ 请提供频道ID\n用法: `/remove_source -1001234567890`")
            return
        
        try:
            channel_id = int(context.args[0])
            if channel_id in self.config['source_channels']:
                self.config['source_channels'].remove(channel_id)
                self.save_config()
                await update.message.reply_text(f"✅ 已移除源频道: `{channel_id}`", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text("❌ 该频道不在源列表中")
        except ValueError:
            await update.message.reply_text("❌ 无效的频道ID")
    
    async def remove_target_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """移除目标频道命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ 请提供频道ID\n用法: `/remove_target -1001234567890`")
            return
        
        try:
            channel_id = int(context.args[0])
            if channel_id in self.config['target_channels']:
                self.config['target_channels'].remove(channel_id)
                self.save_config()
                await update.message.reply_text(f"✅ 已移除目标频道: `{channel_id}`", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text("❌ 该频道不在目标列表中")
        except ValueError:
            await update.message.reply_text("❌ 无效的频道ID")
    
    async def list_sources_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """列出源频道"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        sources = self.config['source_channels']
        if not sources:
            await update.message.reply_text("📢 当前没有配置源频道")
            return
        
        text = "📢 *源频道列表:*\n\n"
        for i, source_id in enumerate(sources, 1):
            text += f"{i}\\. `{source_id}`\n"
        
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    
    async def list_targets_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """列出目标频道"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        targets = self.config['target_channels']
        if not targets:
            await update.message.reply_text("🎯 当前没有配置目标频道")
            return
        
        text = "🎯 *目标频道列表:*\n\n"
        for i, target_id in enumerate(targets, 1):
            text += f"{i}\\. `{target_id}`\n"
        
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    
    async def add_admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """添加管理员命令"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ 请提供用户ID\n用法: `/add_admin 123456789`")
            return
        
        try:
            new_admin_id = int(context.args[0])
            if new_admin_id not in self.config['admins']:
                self.config['admins'].append(new_admin_id)
                self.save_config()
                await update.message.reply_text(f"✅ 已添加管理员: `{new_admin_id}`", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await update.message.reply_text("❌ 该用户已是管理员")
        except ValueError:
            await update.message.reply_text("❌ 无效的用户ID")
    
    async def list_admins_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """列出管理员"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        admins = self.config['admins']
        if not admins:
            await update.message.reply_text("👥 当前没有配置管理员")
            return
        
        text = "👥 *管理员列表:*\n\n"
        for i, admin_id in enumerate(admins, 1):
            text += f"{i}\\. `{admin_id}`\n"
        
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    
    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看设置"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        settings = self.config['forward_settings']
        
        # 安全处理过滤器列表
        filter_types = escape_markdown_v2(', '.join(settings['filter_content_types']) if settings['filter_content_types'] else '无')
        keyword_filters = escape_markdown_v2(', '.join(settings['keyword_filter']) if settings['keyword_filter'] else '无')
        
        settings_text = f"""⚙️ *当前设置*

🕐 *转发延迟:* {settings['delay_seconds']} 秒
📝 *显示来源信息:* {'✅' if settings['add_source_info'] else '❌'}
👤 *保留发送者信息:* {'✅' if settings['preserve_sender'] else '❌'}
🚫 *过滤的内容类型:* {filter_types}
🔑 *关键词过滤:* {keyword_filters}
⚡ *每分钟最大转发数:* {settings['max_forwards_per_minute']}
🖼️ *媒体组超时:* {settings['media_group_timeout']} 秒

📊 *通知设置:*
• 错误通知: {'✅' if self.config['notification_settings']['notify_admin_on_error'] else '❌'}
• 日报: {'✅' if self.config['notification_settings']['daily_report'] else '❌'}"""
        
        await update.message.reply_text(settings_text, parse_mode=ParseMode.MARKDOWN_V2)
    
    async def set_delay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """设置转发延迟"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        if not context.args:
            await update.message.reply_text("❌ 请提供延迟秒数\n用法: `/set_delay 5`")
            return
        
        try:
            delay = int(context.args[0])
            if delay < 0:
                await update.message.reply_text("❌ 延迟时间不能为负数")
                return
            
            self.config['forward_settings']['delay_seconds'] = delay
            self.save_config()
            await update.message.reply_text(f"✅ 转发延迟已设置为 {delay} 秒")
        except ValueError:
            await update.message.reply_text("❌ 无效的数字")
    
    async def toggle_source_info_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """切换来源信息显示"""
        user_id = update.effective_user.id
        if not await self.is_admin(user_id):
            return
        
        current = self.config['forward_settings']['add_source_info']
        self.config['forward_settings']['add_source_info'] = not current
        self.save_config()
        
        status = "开启" if not current else "关闭"
        await update.message.reply_text(f"✅ 来源信息显示已{status}")
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理消息转发"""
        message = update.message
        if not message:
            return
        
        chat_id = message.chat_id
        
        # 记录频道ID到日志（方便获取ID）
        logger.info(f"收到消息来自频道: {chat_id} ({message.chat.title}) - 媒体组ID: {message.media_group_id}")
        
        # 检查是否来自源频道
        if chat_id not in self.config['source_channels']:
            return
        
        self.stats['messages_received'] += 1
        
        # 获取消息类型
        content_type = self.get_message_type(message)
        
        # 检查内容过滤
        if self.should_filter_message(message, content_type):
            logger.info(f"消息被过滤: {content_type}")
            return
        
        # 使用媒体组处理器
        await self.media_group_handler.add_message(message, self.forward_messages_group)
    
    def get_message_type(self, message: Message) -> str:
        """获取消息类型"""
        if message.text:
            return "text"
        elif message.photo:
            return "photo"
        elif message.video:
            return "video"
        elif message.document:
            return "document"
        elif message.audio:
            return "audio"
        elif message.voice:
            return "voice"
        elif message.sticker:
            return "sticker"
        elif message.animation:
            return "animation"
        elif message.location:
            return "location"
        elif message.poll:
            return "poll"
        else:
            return "other"
    
    def should_filter_message(self, message: Message, content_type: str) -> bool:
        """检查消息是否应被过滤"""
        # 内容类型过滤
        if content_type in self.config['forward_settings']['filter_content_types']:
            return True
        
        # 关键词过滤
        if message.text and self.config['forward_settings']['keyword_filter']:
            text_lower = message.text.lower()
            for keyword in self.config['forward_settings']['keyword_filter']:
                if keyword.lower() in text_lower:
                    return True
        
        return False
    
    async def forward_messages_group(self, messages: List[Message]):
        """转发消息组（支持媒体组）"""
        if not messages:
            return
        
        targets = self.config['target_channels']
        if not targets:
            return
        
        # 转发延迟
        delay = self.config['forward_settings']['delay_seconds']
        if delay > 0:
            await asyncio.sleep(delay)
        
        is_media_group = len(messages) > 1 and messages[0].media_group_id
        
        if is_media_group:
            logger.info(f"转发媒体组，包含 {len(messages)} 条消息")
            await self.forward_media_group(messages)
        else:
            logger.info(f"转发单条消息")
            await self.forward_single_message(messages[0])
    
    async def forward_media_group(self, messages: List[Message]):
        """转发媒体组"""
        targets = self.config['target_channels']
        
        for target_id in targets:
            try:
                # 构建媒体组
                media_list = []
                caption_text = None
                
                # 先构建caption
                if self.config['forward_settings']['add_source_info']:
                    caption_text = self.build_caption(messages[0])
                
                for i, message in enumerate(messages):
                    # 只在第一条消息添加caption
                    if i == 0 and caption_text:
                        input_media = self.create_input_media(message, caption_text)
                    else:
                        input_media = self.create_input_media(message)
                    
                    if input_media:
                        media_list.append(input_media)
                
                if media_list:
                    # 发送媒体组
                    sent_messages = await self.application.bot.send_media_group(
                        chat_id=target_id,
                        media=media_list
                    )
                    
                    # 记录成功转发
                    for i, (original_msg, sent_msg) in enumerate(zip(messages, sent_messages)):
                        content_type = self.get_message_type(original_msg)
                        self.log_forward(
                            original_msg.chat_id,
                            target_id,
                            original_msg.message_id,
                            sent_msg.message_id,
                            content_type,
                            original_msg.media_group_id,
                            True,
                            True,
                            None
                        )
                    
                    self.stats['messages_forwarded'] += len(messages)
                    self.stats['media_groups_forwarded'] += 1
                    logger.info(f"媒体组已转发: {messages[0].chat_id} -> {target_id} ({len(messages)}条)")
                
            except Exception as e:
                error_msg = str(e)
                logger.error(f"媒体组转发失败 {messages[0].chat_id} -> {target_id}: {error_msg}")
                
                # 记录失败转发
                for message in messages:
                    content_type = self.get_message_type(message)
                    self.log_forward(
                        message.chat_id,
                        target_id,
                        message.message_id,
                        None,
                        content_type,
                        message.media_group_id,
                        True,
                        False,
                        error_msg
                    )
                
                self.stats['failed_forwards'] += len(messages)
                
                # 通知管理员
                if self.config['notification_settings']['notify_admin_on_error']:
                    await self.notify_admins_error(messages[0], target_id, error_msg)
    
    async def forward_single_message(self, message: Message):
        """转发单条消息"""
        targets = self.config['target_channels']
        content_type = self.get_message_type(message)
        
        for target_id in targets:
            try:
                # 构建标题，如果需要的话
                caption = None
                if self.config['forward_settings']['add_source_info']:
                    caption = self.build_caption(message)
                
                # 使用 copy_message 转发单条消息
                forwarded_message = await self.application.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                    caption=caption
                )
                
                # 记录成功转发
                self.log_forward(
                    message.chat_id,
                    target_id,
                    message.message_id,
                    forwarded_message.message_id,
                    content_type,
                    message.media_group_id,
                    False,
                    True,
                    None
                )
                
                self.stats['messages_forwarded'] += 1
                logger.info(f"消息已转发: {message.chat_id} -> {target_id}")
                
            except Exception as e:
                error_msg = str(e)
                logger.error(f"转发失败 {message.chat_id} -> {target_id}: {error_msg}")
                
                # 记录失败转发
                self.log_forward(
                    message.chat_id,
                    target_id,
                    message.message_id,
                    None,
                    content_type,
                    message.media_group_id,
                    False,
                    False,
                    error_msg
                )
                
                self.stats['failed_forwards'] += 1
                
                # 通知管理员
                if self.config['notification_settings']['notify_admin_on_error']:
                    await self.notify_admins_error(message, target_id, error_msg)
    
    def create_input_media(self, message: Message, caption: str = None):
        """根据消息创建InputMedia对象，可选择性添加caption"""
        try:
            if message.photo:
                # 获取最高质量的照片
                photo = message.photo[-1]
                return InputMediaPhoto(media=photo.file_id, caption=caption)
            elif message.video:
                return InputMediaVideo(media=message.video.file_id, caption=caption)
            elif message.document:
                return InputMediaDocument(media=message.document.file_id, caption=caption)
            elif message.audio:
                return InputMediaAudio(media=message.audio.file_id, caption=caption)
            else:
                logger.warning(f"不支持的媒体类型: {self.get_message_type(message)}")
                return None
        except Exception as e:
            logger.error(f"创建InputMedia失败: {e}")
            return None
    
    def build_caption(self, message: Message) -> str:
        """构建转发消息的说明"""
        original_caption = message.caption or ""
        
        if not self.config['forward_settings']['add_source_info']:
            return original_caption
        
        # 添加来源信息，使用普通文本格式
        chat_title = message.chat.title or str(message.chat.id)
        source_info = f""
        
        if message.from_user and self.config['forward_settings']['preserve_sender']:
            sender_name = message.from_user.full_name
            source_info += f""
        
        # 使用简单的时间格式，避免 Windows 格式化问题
        time_str = message.date.strftime('%Y-%m-%d %H:%M:%S')
        source_info += f""   
        
        return original_caption + source_info   
    
    def log_forward(self, source_chat_id: int, target_chat_id: int, 
                   original_msg_id: int, forwarded_msg_id: int,
                   content_type: str, media_group_id: str, is_media_group: bool,
                   success: bool, error_msg: str):
        """记录转发日志"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO forward_logs 
                (source_chat_id, target_chat_id, original_message_id, 
                 forwarded_message_id, content_type, media_group_id, is_media_group, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (source_chat_id, target_chat_id, original_msg_id,
                  forwarded_msg_id, content_type, media_group_id, is_media_group, success, error_msg))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"记录转发日志失败: {e}")
    
    async def notify_admins_error(self, message: Message, target_id: int, error_msg: str):
        """通知管理员转发错误 - 使用普通文本避免格式问题"""
        chat_title = message.chat.title or str(message.chat.id)
        time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        error_text = f"""❌ 转发失败通知

源频道: {chat_title}
目标频道: {target_id}
错误信息: {error_msg}
时间: {time_str}"""
        
        for admin_id in self.config['admins']:
            try:
                await self.application.bot.send_message(
                    chat_id=admin_id,
                    text=error_text
                )
            except Exception as e:
                logger.error(f"通知管理员失败 {admin_id}: {e}")
    
    def run(self):
        """运行机器人"""
        logger.info("机器人启动中...")
        # 更新媒体组处理器超时时间
        self.media_group_handler.timeout_seconds = self.config['forward_settings']['media_group_timeout']
        self.application.run_polling()

# 配置文件示例
def create_sample_config():
    """创建示例配置文件"""
    sample_config = {
        "bot_token": "YOUR_BOT_TOKEN_HERE",
        "admins": [123456789],  # 替换为你的用户ID
        "source_channels": [],  # 源频道ID列表
        "target_channels": [],  # 目标频道ID列表
    }
    
    with open("config_sample.json", "w", encoding="utf-8") as f:
        json.dump(sample_config, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    # 从环境变量或配置文件获取token
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not TOKEN:
        print("❌ 请设置 TELEGRAM_BOT_TOKEN 环境变量")
        print("💡 示例: set TELEGRAM_BOT_TOKEN=your_bot_token_here")
        create_sample_config()
        print("📝 已创建示例配置文件 config_sample.json")
        exit(1)
    
    # 创建并运行机器人
    bot = TelegramForwardBot(TOKEN)
    bot.run()