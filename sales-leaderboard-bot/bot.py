import os
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ========= CONFIG =========
TOKEN = os.getenv("DISCORD_TOKEN")
LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID", "0"))
TZ = os.getenv("TIMEZONE", "America/Chicago")
POST_HOUR = int(os.getenv("POST_HOUR", "9"))
POST_MINUTE = int(os.getenv("POST_MINUTE", "0"))

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
BOT = commands.Bot(command_prefix="!", intents=INTENTS)

DB_PATH = "sales.db"
MONEY_RE = re.compile(r"\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)")

# ========= DB =========
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        amount REAL NOT NULL,
        ts TEXT NOT NULL
    )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_message ON sales(message_id)")
    return conn

def now_utc(): return datetime.utcnow()

def daterange_bounds_local(day_offset=0):
    tz = ZoneInfo(TZ)
    now_local = datetime.now(tz)
    base = now_local.date() + timedelta(days=day_offset)
    start_local = datetime.combine(base, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))

def parse_amounts(text: str) -> float:
    vals = []
    for m in MONEY_RE.finditer(text or ""):
        try: vals.append(float(m.group(1).replace(",", "")))
        except ValueError: pass
    return round(sum(vals), 2) if vals else 0.0

def insert_sale_row(guild, channel, msg, user, amount):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sales (guild_id, channel_id, message_id, user_id, amount, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (str(guild), str(channel), str(msg), str(user), amount, now_utc().isoformat())
        ); conn.commit()

def update_sale_amount(msg, amount):
    with db() as conn:
        conn.execute("UPDATE sales SET amount=? WHERE message_id=?", (amount, str(msg))); conn.commit()

def delete_sale_by_msg(msg):
    with db() as conn:
        conn.execute("DELETE FROM sales WHERE message_id=?", (str(msg),)); conn.commit()

def sales_by_user_between(start, end):
    with db() as conn:
        cur = conn.execute("""
        SELECT user_id, SUM(amount) FROM sales
        WHERE ts>=? AND ts<? GROUP BY user_id ORDER BY SUM(amount) DESC
        """,(start.isoformat(), end.isoformat()))
        return cur.fetchall()

def total_between(start, end):
    with db() as conn:
        cur = conn.execute("SELECT SUM(amount) FROM sales WHERE ts>=? AND ts<?",(start.isoformat(), end.isoformat()))
        v = cur.fetchone()[0]; return float(v) if v else 0.0

def latest_entry_today(user_id):
    s,e = daterange_bounds_local(0)
    with db() as conn:
        cur = conn.execute("""
        SELECT id,message_id,amount FROM sales
        WHERE user_id=? AND ts>=? AND ts<? ORDER BY ts DESC,id DESC LIMIT 1
        """,(str(user_id), s.isoformat(), e.isoformat()))
        return cur.fetchone()

def delete_row_by_id(row_id):
    with db() as conn:
        conn.execute("DELETE FROM sales WHERE id=?",(row_id,)); conn.commit()

def fmt_money(n): return f"${n:,.2f}"

def lb_lines(rows,guild):
    out=[]
    for i,(uid,total) in enumerate(rows,1):
        m=guild.get_member(int(uid))
        name=m.display_name if m else f"User {uid}"
        out.append(f"**{i}. {name}** — {fmt_money(total)}")
    return "\n".join(out) if out else "_No sales yet_"

# ========= BOT EVENTS =========
@BOT.event
async def on_ready():
    db()
    try: await BOT.tree.sync()
    except Exception as e: print("Sync error:",e)
    print(f"Logged in as {BOT.user}")
    start_scheduler()

@BOT.event
async def on_message(msg):
    if not msg.guild or msg.author.bot: return
    amt=parse_amounts(msg.content)
    if amt>0:
        insert_sale_row(msg.guild.id,msg.channel.id,msg.id,msg.author.id,amt)
        try: await msg.add_reaction("✅")
        except: pass
    await BOT.process_commands(msg)

@BOT.event
async def on_message_edit(before,after):
    if not after.guild or after.author.bot: return
    old = parse_amounts(before.content)
    new = parse_amounts(after.content)
    if old==0 and new>0: insert_sale_row(after.guild.id,after.channel.id,after.id,after.author.id,new)
    elif old>0 and new>0 and old!=new: update_sale_amount(after.id,new)
    elif old>0 and new==0: delete_sale_by_msg(after.id)

# ========= SLASH CMDS =========
@BOT.tree.command(description="Show today's leaderboard.")
async def leaderboard(itx):
    s,e=daterange_bounds_local(0)
    rows=sales_by_user_between(s,e)
    emb=discord.Embed(title="📊 Today’s Sales Leaderboard",
        description=lb_lines(rows,itx.guild),color=0x2b6cb0)
    await itx.response.send_message(embed=emb)

@BOT.tree.command(description="Undo your last sale today.")
async def undo(itx):
    row=latest_entry_today(itx.user.id)
    if not row:
        await itx.response.send_message("No entries found today.",ephemeral=True);return
    rid,mid,amt=row
    delete_row_by_id(rid)
    await itx.response.send_message(f"Removed your last entry ({fmt_money(amt)}).",ephemeral=True)

# ========= DAILY SUMMARY =========
scheduler=None
def start_scheduler():
    global scheduler
    if scheduler:return
    scheduler=AsyncIOScheduler(timezone=ZoneInfo(TZ))
    scheduler.add_job(post_yesterday_summary,CronTrigger(hour=POST_HOUR,minute=POST_MINUTE))
    scheduler.start()

async def post_yesterday_summary():
    if not LEADERBOARD_CHANNEL_ID:return
    ch=BOT.get_channel(LEADERBOARD_CHANNEL_ID)
    if not ch:return
    s,e=daterange_bounds_local(-1)
    rows=sales_by_user_between(s,e)
    total=total_between(s,e)
    tz=ZoneInfo(TZ); y=(datetime.now(tz).date()-timedelta(days=1))
    date_str=y.strftime("%m/%d/%Y")
    await ch.send(f"The total for {date_str} was {fmt_money(total)}")
    desc=lb_lines(rows,ch.guild)
    emb=discord.Embed(title=f"🏁 {y.strftime('%A, %b %-d, %Y')} — Final Leaderboard",
                      description=desc,color=0x16a34a)
    emb.add_field(name="Total Submitted",value=fmt_money(total),inline=False)
    await ch.send(embed=emb)

if __name__=="__main__":
    if not TOKEN: raise RuntimeError("Set DISCORD_TOKEN env var.")
    BOT.run(TOKEN)
