import json
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import asyncio
from telegram import Bot

class BotUtils:
    """机器人工具类"""
    
    @staticmethod
    def get_chat_info(chat_id: int, bot: Bot) -> Optional[Dict]:
        """获取频道/群组信息"""
        try:
            chat = asyncio.run(bot.get_chat(chat_id))
            return {
                'id': chat.id,
                'title': chat.title,
                'type': chat.type,
                'description': chat.description,
                'member_count': getattr(chat, 'member_count', None)
            }
        except Exception as e:
            print(f"获取频道信息失败 {chat_id}: {e}")
            return None
    
    @staticmethod
    def format_file_size(size_bytes: int) -> str:
        """格式化文件大小"""
        if size_bytes == 0:
            return "0B"
        
        size_names = ["B", "KB", "MB", "GB"]
        import math
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_names[i]}"
    
    @staticmethod
    def generate_report(db_path: str, days: int = 7) -> str:
        """生成转发报告"""
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 获取指定天数内的统计
        start_date = datetime.now() - timedelta(days=days)
        
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                content_type
            FROM forward_logs 
            WHERE timestamp >= ?
            GROUP BY content_type
        ''', (start_date,))
        
        stats = cursor.fetchall()
        conn.close()
        
        if not stats:
            return f"📊 **{days}天转发报告**\n\n暂无转发记录"
        
        report = f"📊 **{days}天转发报告**\n\n"
        total_messages = sum(stat[0] for stat in stats)
        total_success = sum(stat[1] for stat in stats)
        
        report += f"📈 **总体统计:**\n"
        report += f"• 总消息数: {total_messages}\n"
        report += f"• 成功转发: {total_success}\n"
        report += f"• 成功率: {(total_success/total_messages*100):.1f}%\n\n"
        
        report += f"📋 **按类型统计:**\n"
        for total, success, content_type in stats:
            success_rate = (success/total*100) if total > 0 else 0
            report += f"• {content_type}: {success}/{total} ({success_rate:.1f}%)\n"
        
        return report

class MessageFilter:
    """消息过滤器"""
    
    def __init__(self, config: Dict):
        self.config = config
    
    def should_forward(self, message, content_type: str) -> bool:
        """判断消息是否应该转发"""
        # 内容类型过滤
        if content_type in self.config.get('filter_content_types', []):
            return False
        
        # 文件大小过滤
        file_size_limit = self.config.get('max_file_size_mb', 50) * 1024 * 1024  # MB to bytes
        if hasattr(message, 'document') and message.document:
            if message.document.file_size > file_size_limit:
                return False
        
        # 关键词过滤
        if message.text:
            blocked_keywords = self.config.get('blocked_keywords', [])
            text_lower = message.text.lower()
            for keyword in blocked_keywords:
                if keyword.lower() in text_lower:
                    return False
        
        # 用户黑名单
        if message.from_user:
            blocked_users = self.config.get('blocked_users', [])
            if message.from_user.id in blocked_users:
                return False
        
        return True

class RateLimiter:
    """速率限制器"""
    
    def __init__(self, max_per_minute: int = 20):
        self.max_per_minute = max_per_minute
        self.requests = []
    
    async def acquire(self) -> bool:
        """获取请求许可"""
        now = datetime.now()
        # 清理一分钟前的请求
        self.requests = [req_time for req_time in self.requests 
                        if now - req_time < timedelta(minutes=1)]
        
        if len(self.requests) >= self.max_per_minute:
            return False
        
        self.requests.append(now)
        return True
    
    async def wait_if_needed(self):
        """如果需要则等待"""
        if not await self.acquire():
            sleep_time = 60 - (datetime.now() - min(self.requests)).seconds
            await asyncio.sleep(sleep_time)