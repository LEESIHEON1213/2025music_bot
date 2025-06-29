import logging
import sys
import discord
import yt_dlp
import json
import asyncio
import subprocess
import random
from discord.ext import commands, tasks
import time
import os
import urllib.parse
import aiohttp
import ffmpeg
import re
from mutagen.mp3 import MP3
import io
from rapidfuzz.fuzz import ratio
import platform
import ctypes
import uuid
import os
import sys
import _strptime
import aiofiles
import shutil
from PIL import Image, ImageSequence
import imageio
from io import BytesIO
import hashlib
from collections import deque
from discord.ui import View, Button


if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


history_file_path = "recent_tracks.json"
recent_tracks = deque()


# 시작 시 로드
try:
    if os.path.exists(history_file_path):
        with open(history_file_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, list):
                recent_tracks.extend(loaded)
    else:
        print("[INFO] recent_tracks.json 없음. 새로 생성 예정.")
except Exception as e:
    print(f"[ERROR] recent_tracks.json 로드 실패: {e}")

def save_recent_track(entry):
    """최근 트랙 저장"""
    # 중복 제거 (동일 URL 제거)
    for i, track in enumerate(recent_tracks):
        if track.get("url") == entry.get("url"):
            del recent_tracks[i]
            break

    # 맨 앞에 추가
    recent_tracks.appendleft(entry)

    try:
        with open(history_file_path, "w", encoding="utf-8") as f:
            json.dump(list(recent_tracks), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] 최근 트랙 저장 실패: {e}")



class MusicState:
    def __init__(self):
        # 기본 상태 설정
        self.is_playing = False
        self.is_paused = False
        self.queue = []
        self.current_song = None
        self.current_duration = 0
        self.current_start_time = None
        self.voice_client = None  # 기본적으로 None으로 설정
        self.repeat = False
        self.repeat_song = None
        self.is_playing_next_song = False
        self.playlist_mode_active = False
        self.previous_queue_length = 0
        self.last_elapsed_time = 0
        self.last_played_embed = None
        self.last_auto_leave_status = "STAY"
        self.is_searching = False
        self.search_queue = asyncio.Queue()
        self.is_embed_active = False
        self.embed_lock = asyncio.Lock()
        self.break_event = asyncio.Event()
        self.break_timer_task = None
        self.is_stopped = False
        self.search_cache = set()
        self.last_selected_playlist = None
        self.cached_channel_id = None
        self.last_playing_state = None
        self.elapsed_time = 0
        self.search_pause_time = None
        self.is_first_batch_processed = False
        self.voice_channel = None
        self.last_queue_length = 0  # 대기열의 마지막 길이
        self.search_tasks = []  # 검색 관련 비동기 작업을 추적하는 리스트
        self.some_condition_to_cancel_task = False

        if self.voice_client:  # 음성 클라이언트가 있을 경우에만 is_playing을 설정
            self.voice_client.is_playing = False

    def is_user_in_voice_channel(self, user):
        """사용자가 음성 채널에 있는지 확인"""
        config = load_config("설정.txt")
        if not self.voice_channel:
            return False
        if not user.voice or not user.voice.channel:
            return False
        return user.voice.channel == self.voice_channel

    async def connect_to_channel(self, channel):
        """음성 채널에 연결"""
        config = load_config("설정.txt")
        try:
            if self.voice_client and self.voice_client.is_connected():
                if self.voice_client.channel == channel:
                    print(f"[INFO] {config['already_connected_channel']}")
                    return
                else:
                    await self.voice_client.disconnect()
                    await asyncio.sleep(1)

            self.voice_client = await channel.connect()
            self.voice_channel = channel
            print(f"[INFO] {config['connected_to_channel'].format(channel_name=channel.name)}")
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            print(f"[ERROR] {config['connection_error'].format(error=e)}")
            await asyncio.sleep(5)
            await self.connect_to_channel(channel)

    async def disconnect(self):
        """음성 채널에서 연결 해제"""
        config = load_config("설정.txt")
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            self.voice_client = None
            self.voice_channel = None
            print(f"[INFO] {config['disconnected_from_channel']}")

    def reset(self):
        """상태 초기화"""
        config = load_config("설정.txt")
        self.queue.clear()
        self.is_playing = False
        self.is_paused = False
        self.current_song = None
        self.current_duration = 0
        self.current_start_time = None
        self.repeat = False
        self.repeat_song = None
        self.is_playing_next_song = False
        self.last_elapsed_time = 0
        self.break_event.clear()
        if self.break_timer_task and not self.break_timer_task.done():
            self.break_timer_task.cancel()
        print(f"[INFO] {config['music_state_reset']}")

    def is_idle(self):
        """봇이 재생을 멈추고 아무 작업도 하지 않는 상태 확인"""
        return not self.is_playing and not self.is_paused and not self.queue and not self.is_searching

    def toggle_repeat(self):
        """반복 모드 토글"""
        config = load_config("설정.txt")
        self.repeat = not self.repeat
        if not self.repeat:
            self.repeat_song = None
        print(f"[INFO] {config['repeat_mode_toggled'].format(state='ON' if self.repeat else 'OFF')}")

    async def check_auto_disconnect(self):
        """대기열이 비었고, 곡이 재생 중이지 않으면 자동 퇴장"""
        config = load_config("설정.txt")
        if len(self.queue) == 0 and not self.is_playing:
            channel_name = get_channel_name()  # 채널 이름 불러오기
            if not channel_name:
                print(f"[ERROR] {config['channel_name_error']}")
                return

            channel = discord.utils.get(bot.get_all_channels(), name=channel_name)
            if channel and self.voice_client and self.voice_client.is_connected():
                embed_color = int(config['embed_color'], 16)  # 색상 코드 변환
                embed = create_embed(config['auto_disconnect_title'], config['auto_disconnect_message'], embed_color)
                await channel.send(embed=embed, delete_after=5)
                await self.voice_client.disconnect()
                print(f"[INFO] {config['auto_disconnect_info']}")

    async def update_queue_length(self):
        """대기열 길이를 업데이트"""
        self.last_queue_length = len(self.queue)

    async def on_queue_change(self):
        """대기열이 1에서 0으로 변할 때 자동 퇴장 체크"""
        if len(self.queue) == 0 and self.last_queue_length == 1:
            await self.check_auto_disconnect()
        await self.update_queue_length()

    def add_search_task(self, task):
        """검색 관련 비동기 작업을 리스트에 추가"""
        self.search_tasks.append(task)

    def cancel_search_tasks(self):
        """모든 검색 관련 비동기 작업 취소"""
        for task in self.search_tasks:
            task.cancel()
            print(f"[INFO] 취소된 검색 작업: {task}")
        self.search_tasks.clear()  # 검색 작업 리스트 초기화



DATA_DIR = os.path.abspath("data")  # 절대경로로 설정
# 봇 재시작 상태 플래그
is_restart_in_progress = False
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)



def save_recent_track(track):
    """recent_tracks.json에 최근 곡을 누적 저장 (무제한, 중복 제거 후 최신으로 갱신)"""
    if not isinstance(track, dict):
        print("[ERROR] 잘못된 track 형식:", track)
        return

    if os.path.exists(history_file_path):
        with open(history_file_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = []
    else:
        data = []

    # 중복 URL 제거
    data = [t for t in data if t.get("url") != track.get("url")]

    # 가장 앞에 추가
    data.insert(0, track)

    with open(history_file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)



def load_recent_tracks():
    if os.path.exists(history_file_path):
        with open(history_file_path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


async def processing_embed(channel, title, description, color):
    embed = discord.Embed(title=title, description=description, color=color)
    return await channel.send(embed=embed)

ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': False,  # 기존엔 단일 영상만 처리했음
        'quiet': True,
        'no-warnings': True,
        'default-search': 'ytsearch',
        'source_address': '0.0.0.0',
        'socket-timeout': 10,
        'geo-bypass': True,
        'skip-download': True,
        'extract-audio': False,
        'write-info-json': False,
        'write-thumbnail': False,
        'writesubtitles': False,
        'write-auto-sub': False,
        'keepvideo': False,
        'extract_flat': True,
        'format_sort': False,
        'merge_output_format': False,
}


def load_config(config_file="설정.txt"):
    """설정 파일을 읽고 값을 반환하는 함수"""
    config = {}
    try:
        with open(config_file, "r", encoding="utf-8") as file:
            for line in file:
                if "=" not in line or not line.strip():
                    continue
                key, value = line.strip().split("=", 1)
                config[key] = value
    except Exception as e:
        print(f"[ERROR] 설정 파일을 읽는 중 오류 발생: {e}")
    return config


# 설정값 불러오기
config = load_config()
root_path = config.get("root", "").strip('"').strip("'")
ffmpeg_path = os.path.abspath(os.path.join(root_path, "ffmpeg.exe"))


# FFmpeg 경로 설정 (현재 디렉토리의 'ffmpeg' 폴더 내부)
ffmpeg_path = os.path.join(os.getcwd(), "ffmpeg", "ffmpeg.exe")

# FFmpeg 경로 존재 확인
if not os.path.isfile(ffmpeg_path):
    raise FileNotFoundError(f"[FFmpeg] '{ffmpeg_path}' 경로에 ffmpeg.exe가 존재하지 않습니다.")

# opus dll 경로 설정
opus_path = os.path.join(os.getcwd(), "libopus.dll")

if os.path.isfile(opus_path):
    discord.opus.load_opus(opus_path)
else:
    raise FileNotFoundError("[ERROR] 'libopus.dll' 파일이 현재 디렉토리에 존재하지 않습니다.")

# FFmpeg 옵션
ffmpeg_opts = {
    'executable': ffmpeg_path,
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -loglevel panic',
    'options': '-vn'
}





class RecentTrackButton(Button):
    def __init__(self, track, index, embed_color):
        super().__init__(label=f"{index + 1}. {track['title'][:50]}", style=discord.ButtonStyle.primary)
        self.track = track
        self.embed_color = embed_color
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        config = load_config("설정.txt")
        user = interaction.user
        voice_state = user.voice

        if not interaction.response.is_done():
            await interaction.response.defer()

        # ❗ 음성 채널 미참여
        if not voice_state or not voice_state.channel:
            try:
                if interaction.message and not interaction.message.flags.ephemeral:
                    await interaction.message.delete()
            except discord.NotFound:
                pass

            try:
                if music_state.recent_tracks_message:
                    await music_state.recent_tracks_message.delete()
                    music_state.recent_tracks_message = None
            except discord.NotFound:
                music_state.recent_tracks_message = None

            try:
                msg = await interaction.followup.send(
                    embed=create_embed("❗ 음성 채널에 먼저 들어가 주세요.", self.embed_color)
                )
                await asyncio.sleep(3)
                await msg.delete()
            except discord.NotFound:
                pass
            return

        # 🎧 음성 채널 연결
        if not music_state.voice_client or not music_state.voice_client.is_connected():
            try:
                if interaction.message and not interaction.message.flags.ephemeral:
                    await interaction.message.delete()
            except discord.NotFound:
                pass

            try:
                if music_state.recent_tracks_message:
                    await music_state.recent_tracks_message.delete()
                    music_state.recent_tracks_message = None
            except discord.NotFound:
                music_state.recent_tracks_message = None

            await music_state.connect_to_channel(voice_state.channel)

        # ➕ 곡 추가
        try:
            if 'playlist' in self.track:
                song = await add_song_to_queue_v2(interaction.channel, self.track)
            else:
                song = await add_song_to_queue(interaction.channel, self.track['url'])
        except Exception as e:
            print(f"[ERROR] 곡 추가 중 오류: {e}")
            song = None

        if song:
            if music_state.voice_client and not music_state.voice_client.is_playing():
                await play_next_song(interaction.channel, music_state)
        else:
            try:
                msg = await interaction.followup.send(
                    embed=create_embed("⚠️ 이미 큐에 있거나 추가에 실패했습니다.", self.embed_color)
                )
                await asyncio.sleep(3)
                await msg.delete()
            except discord.NotFound:
                pass

        # ✅ 버튼 누른 후 최근 트랙 메시지를 닫음
        try:
            if music_state.recent_tracks_message:
                await music_state.recent_tracks_message.delete()
                music_state.recent_tracks_message = None
        except discord.NotFound:
            music_state.recent_tracks_message = None


# RecentTracksView: 최근 재생 목록을 포함한 버튼 뷰
class RecentTracksView(View):
    def __init__(self, tracks, embed_color):
        super().__init__(timeout=None)
        self.embed_color = embed_color
        for i, track in enumerate(tracks):
            self.add_item(RecentTrackButton(track, i, embed_color))


# ⏱️ 최근 재생 기록 이모지 반응 핸들러 예시
async def handle_recent_tracks(reaction, user, music_state, embed_color):
    if hasattr(music_state, "recent_tracks_message") and music_state.recent_tracks_message:
        try:
            await music_state.recent_tracks_message.delete()
        except discord.NotFound:
            pass
        music_state.recent_tracks_message = None
        return

    recent_tracks = load_recent_tracks()

    if not recent_tracks:
        msg = await reaction.message.channel.send(
            embed=create_embed("최근 재생 기록", "기록이 없습니다.", embed_color)
        )
        await msg.delete(delay=3)
        return

    description = "\n".join([
        f"{i + 1}. [{track['title']}]({track['url']})" +
        (f"\n   🎶 재생목록: [{track['playlist']['name']}]({track['playlist']['url']})"
         if 'playlist' in track else "")
        for i, track in enumerate(recent_tracks[:10])
    ])

    embed = create_embed("최근 재생 기록", description, embed_color)
    view = RecentTracksView(recent_tracks[:10], embed_color)
    music_state.recent_tracks_message = await reaction.message.channel.send(embed=embed, view=view)












        

# 봇 토큰 읽기
BOT_TOKEN = config.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("설정 파일에 봇 토큰이 없습니다.")

# 유튜브 API 키 읽기
api_key = config.get('api_key')
if not api_key:
    raise ValueError("설정 파일에 유튜브 API 키가 없습니다.")

# 채널명 읽기
CHANNEL_NAME = config.get("CHANNEL_NAME")
if not CHANNEL_NAME:
    raise ValueError("설정 파일에 채널명이 없습니다.")


TRIGGER_CHANNEL_NAME = config.get("TRIGGER_CHANNEL_NAME")
TEMP_CATEGORY_NAME = config.get("TEMP_CATEGORY_NAME")
TEMP_CHANNEL_NAME = config.get("TEMP_CHANNEL_NAME")
EMOJI_LINK_MAP_FILE = "emoji_links.json"


def get_channel_name():
    try:
        return CHANNEL_NAME
    except Exception as e:
        print(f"[ERROR] 채널명 불러오기 실패: {e}")
        return None

async def print_message(channel, message):
    """채널에 메시지를 출력하는 함수"""
    try:
        # 채널 이름 가져오기
        channel_name = get_channel_name()

        if channel_name is None:
            raise ValueError("채널 이름을 설정 파일에서 읽을 수 없습니다.")

        # 채널 이름과 비교하여 일치하면 메시지를 전송
        if channel and channel.name == channel_name:
            await channel.send(message)  # 채널 이름이 일치하면 메시지 전송
        else:
            print(f"채널 '{channel_name}'과 일치하지 않음. 메시지를 콘솔에 출력: {message}")

    except Exception as e:
        print(f"[ERROR] 메시지 출력 중 오류 발생: {e}")



EMOJI_CHANNEL_NAME = "😀┃이모지"
MAX_EMOJIS = 50
TEMP_CHANNELS = {}  # 생성된 채널 관리
channel_count = 1  # 채널 번호
MAX_RETRIES = 3
queue_lock = asyncio.Lock()
last_played_embed = None
MAX_SONGS = 50 #더 높게 설정해도 유튜브 API 정책으로 한번에 50곡이 한계임.
channel_name = get_channel_name()
config = load_config()  # 설정 파일을 불러옵니다.

music_state = MusicState()
EMOJI_JSON_PATH = "emoji.json"
EMOJI_FOLDER = "emoji"
TARGET_SIZE = (128, 128)

if not os.path.exists(EMOJI_FOLDER):
    os.makedirs(EMOJI_FOLDER)
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.reactions = True
intents.members = True
intents.voice_states = True
emoji_map = set()
bot = commands.Bot(command_prefix="!", intents=intents)

# 이모지 폴더 내 파일들 중 확장자 제거한 이름만 저장
for filename in os.listdir(EMOJI_FOLDER):
    name, ext = os.path.splitext(filename)
    if ext.lower() in ['.gif', '.png', '.jpg']:
        emoji_map.add(name)

#-----------------------------------------------------------------------------------------------


@bot.event
async def on_member_join(member):
    config = load_config("설정.txt")

    # MAIN_CHANNEL 키를 가져와 해당 이름의 채널 찾기
    channel_name = config.get("MAIN_CHANNEL", None)
    if not channel_name:
        print("⚠️ 설정 파일에서 MAIN_CHANNEL 값을 찾을 수 없습니다.")
        return

    guild = member.guild
    channel = discord.utils.get(guild.text_channels, name=channel_name)

    if not channel:
        print(f"⚠️ '{channel_name}' 채널을 찾을 수 없습니다.")
        return

    # embed_color 값을 가져와 변환 (없으면 기본값 None)
    embed_color = config.get("embed_color", None)
    if embed_color:
        try:
            embed_color = int(embed_color, 16)
        except ValueError:
            print(f"⚠️ 잘못된 색상 코드: {embed_color}")
            embed_color = None
    else:
        embed_color = None

    # 유저 표시 이름 (닉네임이 있으면 닉네임, 없으면 유저명)
    user_display_name = member.display_name or member.name

    # 프로필 사진 URL (서버 전용 아바타가 있으면 그것도 포함됨)
    avatar_url = member.display_avatar.url

    # 임베드 생성
    embed = discord.Embed(
        title="환영합니다!",
        description=f"**{user_display_name}** 님이 서버에 입장하셨습니다 🎉",
        color=embed_color or discord.Color.blue()
    )
    embed.set_thumbnail(url=avatar_url)  # 프로필 사진 추가

    await channel.send(embed=embed)















    

@bot.event
async def on_voice_state_update(member, before, after):
    global channel_count
    guild = member.guild

    # 길드에서 트리거 채널 찾기 (이름 기반)
    trigger_channel = discord.utils.get(guild.voice_channels, name=TRIGGER_CHANNEL_NAME)
    if not trigger_channel:
        print(f"[ERROR] 트리거 채널 '{TRIGGER_CHANNEL_NAME}' 찾을 수 없음")
        return

    # 길드에서 카테고리 찾기 (이름 기반)
    category = discord.utils.get(guild.categories, name=TEMP_CATEGORY_NAME)
    if not category:
        print(f"[ERROR] 카테고리 '{TEMP_CATEGORY_NAME}' 찾을 수 없음")
        return

    # 사용자가 트리거 채널에 입장하면 새 채널 생성
    if after.channel and after.channel == trigger_channel:
        # 채널 이름에 번호 추가
        new_channel_name = f"{TEMP_CHANNEL_NAME} {channel_count}"
        channel_count += 1  # 번호 증가

        # 새 음성 채널 생성 (카테고리 지정)
        new_channel = await guild.create_voice_channel(
            name=new_channel_name,
            category=category
        )

        # 🔐 권한 동기화 (카테고리 기준으로)
        await new_channel.edit(sync_permissions=True)

        # 생성된 채널 저장 및 사용자 이동
        TEMP_CHANNELS[new_channel.id] = new_channel
        await member.move_to(new_channel)

    # 사용자가 임시 채널을 떠날 경우 삭제 확인
    if before.channel and before.channel.id in TEMP_CHANNELS:
        channel = before.channel

        if len(channel.members) == 0:
            await channel.delete()
            del TEMP_CHANNELS[channel.id]





#전용 채널에서 입력되는 메시지로 검색을 시도하게 하는 이벤트
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    if await handle_custom_emoji_message(message):
        return

    content = message.content.strip()
    config = load_config("설정.txt")  # 대사 파일 로드
    embed_color = config.get('embed_color')  # 대사에서 색상 코드 불러오기

    if embed_color is not None:
        try:
            embed_color = int(embed_color, 16)
        except ValueError:
            embed_color = None  # 유효하지 않으면 embed_color를 None으로 설정
    else:
        embed_color = None  # embed_color가 None이면 None으로 설정

    channel_name = get_channel_name()  # 채널 이름 불러오기

    if message.attachments:
        if message.channel.name == channel_name:
            if message.author.voice and message.author.voice.channel:
                if music_state.voice_client and music_state.voice_client.is_connected():
                    print("[INFO] 이미 음성 채널에 연결되어 있습니다.")
                else:
                    await music_state.connect_to_channel(message.author.voice.channel)  # 연결 중복 방지
            await process_uploaded_audio(message)
        return

    if content.startswith("!") and message.channel.name != channel_name:
        await bot.process_commands(message)
        return

    if message.channel.name == channel_name:
        if content.startswith("!"):
            await bot.process_commands(message)
            return

        # ✅ 통화방 입장 여부 체크 (모든 로직 전에)
        if not (message.author.voice and message.author.voice.channel):
            not_voice_msg = config.get('not_voice', "먼저 통화방에 참여해주세요.")
            await clean_channel_message(message.channel)
            await message.channel.send(embed=create_embed(not_voice_msg), delete_after=3)
            return

        # ✅ 검색어 입력 시 바로 삭제
        if content:
            try:
                await message.delete()
            except discord.errors.NotFound:
                pass  # 이미 삭제된 경우 무시

        # ✅ 유튜브 재생목록 처리
        if "playlist?list=" in content or "&list=" in content:
            music_state.is_searching = True
            try:
                playlist_id = extract_playlist_id(content)
                playlist_links = await fetch_playlist_data(content, api_key, message) if playlist_id else await extract_playlist_links(content, api_key)

                if not playlist_links:
                    raise ValueError("곡을 가져오지 못했어요.")

                playlist_titles = [song["title"] for song in playlist_links]
                await fetch_songs_in_parallel(playlist_titles, message, message.author)
                await remove_duplicates_from_queue()

            except Exception as e:
                print(f"[ERROR] 재생목록 처리 중 오류 발생: {e}")

            finally:
                music_state.is_searching = False

            return

        # ✅ 유튜브 단일 영상 처리
        if content.startswith("http") and ("youtube.com" in content or "youtu.be" in content):
            if "watch?v=" in content or "youtu.be/" in content:
                music_state.is_searching = True
                processing_embed = await message.channel.send(embed=create_embed(config.get('searching_message'), config.get('waiting_message'), embed_color))

                try:
                    if message.author.voice and message.author.voice.channel:
                        if music_state.voice_client and music_state.voice_client.is_connected():
                            print("[INFO] 이미 음성 채널에 연결되어 있습니다.")
                        else:
                            await music_state.connect_to_channel(message.author.voice.channel)  # 연결 중복 방지

                    song = await add_song_to_queue(message.channel, content, send_embed=True)
                    await processing_embed.delete()

                    if song and not music_state.is_playing and not music_state.is_playing_next_song:
                        await play_next_song(message.channel, music_state)

                except Exception:
                    await processing_embed.delete()
                    return

                finally:
                    music_state.is_searching = False

                return

        # ✅ 일반 검색 (유튜브 검색어 텍스트)
        if not content.startswith("http") and "youtube.com" not in content and "youtu.be" not in content:
            music_state.is_searching = True
            processing_embed = await message.channel.send(embed=create_embed(config.get('searching_message'), config.get('waiting_message'), embed_color))

            try:
                if message.author.voice and message.author.voice.channel:
                    if music_state.voice_client and music_state.voice_client.is_connected():
                        print("[INFO] 이미 음성 채널에 연결되어 있습니다.")
                    else:
                        await music_state.connect_to_channel(message.author.voice.channel)  # 연결 중복 방지

                song = await add_song_to_queue(message.channel, content, send_embed=True)
                await processing_embed.delete()

                if song and not music_state.is_playing and not music_state.is_playing_next_song:
                    await play_next_song(message.channel, music_state)

            except Exception as e:
                await processing_embed.delete()
                await message.channel.send(embed=create_embed(config.get('search_error_title'), f"{config.get('search_error_message')} {e}", embed_color), delete_after=3)
                return

            finally:
                music_state.is_searching = False

            return

        # 🚫 지원하지 않는 링크 처리
        if content.startswith("http"):
            await message.channel.send(embed=create_embed(config.get('invalid_link_title'), config.get('invalid_link_message'), embed_color), delete_after=4)
            return

#검색어에서 링크를 추출하는 함수
async def extract_playlist_links(url, api_key=None):
    """yt-dlp를 사용하여 유튜브 재생목록에서 비디오 링크들을 추출"""
    try:
        # yt-dlp 옵션 설정
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,  # 플랫(단순화된) 형식으로 비디오 정보만 가져옴
            'force_generic_extractor': True,  # 더 강제적으로 다른 방법을 사용하여 추출
            'noplaylist': False,  # 플레이리스트 지원
        }

        # yt-dlp 객체 생성
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(url, download=False)  # 다운로드 하지 않고 정보만 추출

            if 'entries' not in result:
                print("[ERROR] 재생목록에서 비디오 항목을 찾을 수 없습니다.")
                return None

            # 링크만 추출
            links = [entry['url'] for entry in result['entries'] if entry.get('url')]  # URL 추출
            if not links:
                print("[ERROR] 유효한 비디오 링크가 없습니다.")
                return None
            return links

    except Exception as e:
        print(f"[ERROR] yt-dlp를 사용하여 재생목록 링크 추출 중 오류 발생: {e}")
        return None

#자동입장 작업할 때 호출 되는 함수
async def connect_to_channel(self, channel):
    """음성 채널에 연결"""
    config = load_config("설정.txt")  # 메시지 로드
    try:
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel == channel:
                print(f"[INFO] {config['already_connected_channel']}")
                return  # 이미 같은 채널에 연결되어 있으면 아무 작업도 하지 않음
            else:
                await self.voice_client.disconnect()  # 기존에 연결된 채널에서 분리
                await asyncio.sleep(1)  # 잠시 대기 후 새 채널에 연결

        # 새로운 채널에 연결
        self.voice_client = await channel.connect()
        self.voice_channel = channel  # 채널 정보 업데이트
        print(f"[INFO] {config['connected_to_channel'].format(channel_name=channel.name)}")
    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
        print(f"[ERROR] {config['connection_error'].format(error=e)}")
        await asyncio.sleep(5)
        await self.connect_to_channel(channel)  # 실패 시 재시도

async def ensure_voice_channel_connection(user, music_state):
    """음성 채널에 연결이 되어 있는지 확인하고, 연결되지 않았다면 연결 시도"""
    if not music_state.voice_client:
        # 음성 채널에 연결되지 않은 경우
        if user.voice and user.voice.channel:
            # 사용자가 음성 채널에 있고, 음성 채널에 연결된 경우
            print(f"[INFO] 음성 채널에 연결 시도: {user.voice.channel.name}")
            await music_state.connect_to_channel(user.voice.channel)  # 수정된 connect_to_channel 사용
            print("[INFO] 음성 채널에 연결되었습니다.")
        else:
            print("[ERROR] 음성 채널에 연결할 수 없습니다. 사용자가 음성 채널에 없습니다.")
            return False  # 연결 실패
    return True  # 연결 성공

async def check_voice_channel(user, channel):
    """사용자가 음성 채널에 연결되어 있는지 확인"""
    if not user.voice or not user.voice.channel:
        return False
    return True


async def handle_custom_emoji_message(message):
    emoji_data = load_emoji_json()
    config = load_config("설정.txt")
    emoji_store_channel_name = config.get("emoji_store_channel")

    # 이모지 저장 전용 채널에서는 아무 처리하지 않음
    if message.channel.name == emoji_store_channel_name:
        return False

    for emoji in message.guild.emojis:
        if f"<:{emoji.name}:{emoji.id}>" in message.content or f":{emoji.name}:" in message.content:
            if emoji.name in emoji_data:
                try:
                    await message.delete()
                except:
                    pass

                await message.channel.send(f"{message.author.display_name}의 이모지:")
                await message.channel.send(emoji_data[emoji.name])
                return True  # 처리됨
    return False  # 이모지 아님



@bot.command()
async def 게임(ctx):
    await ctx.message.delete()
    config = load_config("설정.txt")
    emoji_role_data = config.get("make_role", "")
    civil_role_name = config.get("civil_role", "").strip()
    embed_color = int(config.get("embed_color", "0xFFC0CB"), 16)
    image_url = config.get("image_set", "")

    # 역할 이모지-이름 파싱
    items = [item.strip() for item in emoji_role_data.split(",") if item.strip()]
    emoji_role_pairs = []
    for item in items:
        emoji = item[0]
        role_name = item[1:].strip()
        emoji_role_pairs.append((emoji, role_name))

    # 역할 생성 또는 재사용
    guild = ctx.guild
    roles_created = {}
    for emoji, role_name in emoji_role_pairs:
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name)
        roles_created[emoji] = role

    # civil 역할 생성 또는 재사용
    civil_role = None
    if civil_role_name:
        civil_role = discord.utils.get(guild.roles, name=civil_role_name)
        if not civil_role:
            civil_role = await guild.create_role(name=civil_role_name)

    # 채널 생성 또는 재사용
    channel_name = "🫴┃역할부여"
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                read_messages=True, send_messages=False
            )
        }
        channel = await guild.create_text_channel(channel_name, overwrites=overwrites)

    # 기존 메시지 삭제
    async for msg in channel.history(limit=100):
        await msg.delete()

    # 임베드 전송
    embed = discord.Embed(
        title="반응을 누르고 역할을 받으세요!",
        description="반응을 누르시면 게임 장르별로 역할을 받을 수 있습니다.\n해당 게임의 정모시에 호출을 받을 수 있습니다.\n(다시 누르시면 역할이 회수됩니다.)",
        color=embed_color
    )
    embed.set_image(url=image_url)
    embed_message = await channel.send(embed=embed)

    # 반응 추가
    for emoji, _ in emoji_role_pairs:
        await embed_message.add_reaction(emoji)

    # 메시지 ID 및 역할 저장
    bot.role_embed_message_id = embed_message.id
    bot.role_emoji_map = roles_created
    bot.role_embed_channel_id = channel.id
    bot.civil_role = civil_role










async def cache_role_embed_messages(bot):
    """모든 서버에서 역할부여 embed 메시지 및 역할 맵, civil 역할 강제로 캐싱"""
    config = load_config("설정.txt")
    target_channel_name = "🫴┃역할부여"
    target_title = "반응을 누르고 역할을 받으세요!"

    for guild in bot.guilds:
        try:
            channel = discord.utils.get(guild.text_channels, name=target_channel_name)
            if not channel:
                continue

            async for message in channel.history(limit=100):
                if message.embeds and message.embeds[0].title == target_title:
                    # 메시지 캐싱
                    bot._connection._messages.append(message)
                    bot.role_embed_message_id = message.id
                    bot.role_embed_channel_id = channel.id

                    # 역할맵 캐싱: 역할 이름과 이모지 데이터 config에서 불러와 동기화
                    emoji_role_data = config.get("make_role", "")
                    civil_role_name = config.get("civil_role", "").strip()

                    items = [item.strip() for item in emoji_role_data.split(",") if item.strip()]
                    emoji_role_pairs = []
                    for item in items:
                        emoji = item[0]
                        role_name = item[1:].strip()
                        emoji_role_pairs.append((emoji, role_name))

                    roles_created = {}
                    for emoji, role_name in emoji_role_pairs:
                        role = discord.utils.get(guild.roles, name=role_name)
                        if role:
                            roles_created[emoji] = role
                    bot.role_emoji_map = roles_created

                    # civil 역할 캐싱
                    civil_role = None
                    if civil_role_name:
                        civil_role = discord.utils.get(guild.roles, name=civil_role_name)
                    bot.civil_role = civil_role

                    break
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            continue






@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    config = load_config("설정.txt")
    emoji_store_channel_name = config.get("emoji_store_channel")
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    emoji = str(payload.emoji)

    if payload.message_id != getattr(bot, "role_embed_message_id", None):
        return

    channel = bot.get_channel(payload.channel_id)
    if channel.name == emoji_store_channel_name:
        return

    # 반응 역할 처리
    role = bot.role_emoji_map.get(emoji)
    if role:
        if role in member.roles:
            await member.remove_roles(role)
        else:
            await member.add_roles(role)

    # civil 역할이 있을 경우에만 무조건 추가 (반응과 무관, 제거 안 됨)
    civil_role = getattr(bot, "civil_role", None)
    if civil_role != "" and civil_role not in member.roles:
        await member.add_roles(civil_role)

    # 반응 제거
    message = await channel.fetch_message(payload.message_id)
    await message.remove_reaction(payload.emoji, member)














async def process_uploaded_audio(message):
    """🎵 업로드된 오디오 파일을 분석하여 음악 대기열에 추가 후 즉시 삭제"""
    config = load_config('설정.txt')

    if not message.attachments:
        print(f"[ERROR] {config['no_audio_file']}")
        return

    file = message.attachments[0]
    valid_extensions = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac")

    if not file.filename.lower().endswith(valid_extensions):
        await message.channel.send(embed=create_embed("오류", config['invalid_audio_format'], int(config['embed_color'], 16)), delete_after=4)
        return

    processing_embed = await message.channel.send(embed=create_embed(config['processing_audio'], "", int(config['embed_color'], 16)))

    # ✅ 1. 파일 다운로드 (UUID로 중복 방지)
    os.makedirs("downloads", exist_ok=True)
    unique_filename = f"{uuid.uuid4()}_{file.filename}"
    file_path = f"downloads/{unique_filename}"
    await file.save(file_path)

    try:
        # ✅ 2. 오디오 길이 측정
        audio = MP3(file_path)
        duration = int(audio.info.length)

        if duration < 1:
            raise ValueError("유효한 오디오 파일이 아닙니다.")

        title = os.path.splitext(os.path.basename(file_path))[0]

        # ✅ 3. 중복 확인
        if any(song["url"] == file_path for song in music_state.queue):
            print(f"[INFO] 이미 큐에 있는 로컬 파일입니다: {file_path}")
            await processing_embed.delete()
            return

        song_info = {
            "title": title,
            "url": file_path,
            "duration": duration,
            "is_local": True,
            "thumbnail_url": config['thumbnail_url'],
        }
        music_state.queue.append(song_info)
        await processing_embed.delete()

        if not music_state.is_playing:
            await play_next_song(message.channel, music_state)

    except Exception as e:
        await processing_embed.delete()
        await message.channel.send(embed=create_embed(config['audio_analysis_error'].format(error=e), "", int(config['embed_color'], 16)), delete_after=4)


def get_audio_duration(file_path):
    """🔍 `ffmpeg-python`을 사용하여 오디오 파일의 길이를 가져옴"""
    try:
        probe = ffmpeg.probe(file_path)
        duration = float(probe["format"]["duration"])
        return duration
    except Exception as e:
        print(f"[ERROR] {config['audio_duration_error'].format(error=e)}")
        return -1

async def convert_mp3_to_wav(file_path):
    """MP3 파일을 WAV로 변환 (비디오 스트림이 있는 경우 대응)"""
    output_file = file_path.replace(".mp3", ".wav")
    try:
        (
            ffmpeg
            .input(file_path, vn=True)  # 비디오 스트림 제거
            .output(output_file, format="wav", acodec="pcm_s16le", ar="44100")
            .run(overwrite_output=True)
        )
        return output_file
    except Exception as e:
        print(f"[ERROR] MP3 변환 실패: {e}")
        return None

async def cleanup_audio_file(file_path):
    """🚀 사용 완료된 오디오 파일 즉시 삭제"""
    try:
        await asyncio.sleep(2)
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"[INFO] 오디오 파일 삭제 완료: {file_path}")
    except Exception as e:
        print(f"[ERROR] 오디오 파일 삭제 실패: {e}")


async def add_song_to_queue(channel, song_url, send_embed=True):
    """유튜브 곡을 대기열에 추가"""
    if not isinstance(channel, discord.abc.Messageable):
        print(f"[ERROR] add_song_to_queue()에서 오류 발생: 'channel'이 유효하지 않음. 타입: {type(channel)}")
        return None

    music_state.is_searching = True

    try:
        song_url = clean_youtube_url(song_url)
        print(f"[DEBUG] 정리된 유튜브 URL: {song_url}")

        is_link_search = song_url.startswith("http")
        song = await search_youtube(song_url, is_link_search=is_link_search)

        if not song or isinstance(song, bool):
            print(f"[ERROR] 곡 검색 실패: {song_url}, 반환값: {song}")
            if send_embed:
                config = load_config("설정.txt")
                embed_color = int(config.get('embed_color'), 16)
                await channel.send(embed=create_embed(config.get('search_failed_title'),
                                                      f"'{song_url}' {config.get('search_failed_message')}", embed_color))
            return None

        music_state.queue.append(song)
        music_state.search_cache.add(song_url)

        # 🔁 반복 모드 아닐 때만 최근 재생곡 저장
        if not music_state.repeat:
            entry = {
                "title": song.get("title", "Unknown Title"),
                "url": song.get("url", song_url)
            }

            if 'playlist' in song:
                entry["playlist"] = {
                    "name": song["playlist"]["name"],
                    "url": song["playlist"]["url"]
                }

            save_recent_track(entry)

        return song

    except Exception as e:
        print(f"[ERROR] add_song_to_queue()에서 오류 발생: {e}")
        if send_embed:
            config = load_config("설정.txt")
            embed_color = int(config.get('embed_color'), 16)
            await channel.send(embed=create_embed(config.get('error_title'),
                                                  f"'{song_url}' {config.get('error_message')} {e}", embed_color))
        return None

    finally:
        music_state.is_searching = False


async def add_song_to_queue_v2(channel, song_info, playlist_name=None):
    if not isinstance(channel, discord.abc.Messageable):
        print(f"[ERROR] add_song_to_queue_v2()에서 오류 발생: 'channel'이 유효하지 않음. 타입: {type(channel)}")
        return None

    try:
        if isinstance(song_info, str):
            song_info = {'url': song_info, 'title': 'Unknown Title', 'artist': 'Unknown Artist'}

        # 재생목록이 있다면 각 곡을 반복 처리
        if 'playlist_songs' in song_info and isinstance(song_info['playlist_songs'], list):
            for single_song_info in song_info['playlist_songs']:
                # 재귀 호출하거나 내부 로직 복사해서 처리해도 됨
                await add_song_to_queue_v2(channel, single_song_info, playlist_name=song_info.get('playlist_name'))
            return None

        song_url = song_info['url']
        song = await search_youtube(song_url, is_link_search=True)

        if not song or isinstance(song, bool):
            return None

        if any(song["url"] == s["url"] for s in music_state.queue):
            return None

        # 🎶 재생목록 정보가 있으면 song에 추가
        if playlist_name:
            song["playlist"] = {
                "name": playlist_name,
                "url": song_info.get("playlist_url", "")
            }
        elif 'playlist' in song_info:
            song["playlist"] = {
                "name": song_info["playlist"]["name"],
                "url": song_info["playlist"]["url"]
            }

        music_state.queue.append(song)
        music_state.search_cache.add(song_url)

        # 🔁 반복 모드 아닐 때만 최근 재생곡 저장
        if not music_state.repeat:
            entry = {
            "title": song.get("title", "Unknown Title"),
            "url": song.get("url", song_url)
    }

    # 플레이리스트 정보가 완전할 경우만 저장
            playlist = song.get("playlist")
            if playlist and playlist.get("name") and playlist.get("url"):
                entry["playlist"] = {
                "name": playlist["name"],
                "url": playlist["url"]
        }

            save_recent_track(entry)

        return song

    except Exception as e:
        print(f"[ERROR] add_song_to_queue_v2()에서 오류 발생: {e}")
        return None

    finally:
        music_state.is_searching = False




#유튜브 링크에서 필요없는 불순물 부분 제거하고 반환하는 함수
def clean_youtube_url(url):
    """🎵 유튜브 URL에서 불필요한 매개변수를 제거하여 올바른 형식으로 반환"""
    if "youtu.be/" in url:
        # youtu.be 단축 URL에서 영상 ID 추출
        match = re.search(r"youtu\.be/([a-zA-Z0-9_-]+)", url)
        if match:
            return f"https://www.youtube.com/watch?v={match.group(1)}"

    elif "watch?v=" in url:
        # youtube.com/watch?v= URL에서 영상 ID 추출
        match = re.search(r"v=([a-zA-Z0-9_-]+)", url)
        if match:
            return f"https://www.youtube.com/watch?v={match.group(1)}"

    return url  # 변환이 필요 없는 경우 그대로 반환

#유튜브 검색을 실시하는 주요 함수
async def search_youtube(query, is_link_search=False, retry_count=0):
    """유튜브에서 영상 검색 및 URL 반환 (링크는 URL 그대로, 텍스트는 텍스트 검색)"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,  # 기본적으로 단일 영상 검색
        'quiet': True,  # 출력 최소화
        'no-warnings': True,
        'default-search': 'ytsearch',
        'extract_flat': True,  # 검색 모드 최적화
        'skip-download': True,
        'socket-timeout': 10,
        'geo-bypass': True,
        'source_address': '0.0.0.0',  # 네트워크 우회 방지
    }

    try:
        print(f"[DEBUG] 유튜브 검색 시도({retry_count + 1}/{MAX_RETRIES}): {query}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = None

            if is_link_search:  # 링크 검색일 경우
                if not query.startswith("https://www.youtube.com/watch?v="):
                    print(f"[ERROR] 잘못된 유튜브 URL: {query}")
                    raise Exception("잘못된 유튜브 URL")

                # URL을 그대로 사용하여 검색
                result = ydl.extract_info(query, download=False)
                if not result or 'title' not in result:
                    print(f"[ERROR] 유튜브 URL 검색 실패: {query}")
                    raise Exception("URL 검색 실패")

            else:  # 텍스트 검색일 경우
                results = ydl.extract_info(f"ytsearch:{query}", download=False)
                if not results or 'entries' not in results or not results['entries']:
                    print(f"[ERROR] 유튜브 텍스트 검색 결과 없음: {query}")
                    raise Exception("검색 결과 없음")
                result = results["entries"][0]  # 첫 번째 검색 결과 선택

        # 유효한 검색 결과 검사
        if not result or 'url' not in result:
            print(f"[ERROR] 유튜브 검색 실패: 검색 결과 없음")
            raise Exception("유효한 검색 결과 없음")

        video_url = result.get("webpage_url") or result.get("url")
        if not video_url:
            print(f"[ERROR] 유효한 비디오 URL을 찾을 수 없음: {query}")
            raise Exception("유효한 비디오 URL 없음")

        return {
            'title': result.get('title', '제목 없음'),
            'url': video_url,
            'thumbnail_url': result.get('thumbnails', [{}])[-1].get('url', ''),
            'duration': result.get('duration', 0),
        }

    except Exception as e:
        print(f"[ERROR] 검색 실패({retry_count + 1}/{MAX_RETRIES}): {query}, 오류: {e}")
        if retry_count >= MAX_RETRIES - 1:
            return None  # 최대 재시도 횟수 초과 시 실패 처리
        await asyncio.sleep(2)  # 재시도 전에 잠시 대기
        return await search_youtube(query, is_link_search, retry_count + 1)  # 재시도

async def process_and_add_songs(song_batch, message, send_embed=True):
    """병렬로 곡들을 추가하고 각 곡을 한 번에 하나씩 처리"""
    tasks = []

    for song in song_batch:
        task = asyncio.create_task(
            add_song_to_queue_v2(message.channel, song["url"], send_embed)
        )
        tasks.append(task)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results

async def fetch_songs_in_parallel(song_list, message, playlist_name=None):
    """유튜브 재생목록 병렬 처리 후 전부 추가된 후에만 재생 시작"""
    config = load_config("설정.txt")
    if playlist_name is None:
        playlist_name = config.get("unknown_playlist_name")

    # 안내 임베드 메시지 출력
    processing_embed_message = await processing_embed(
        message.channel,
        config.get("searching_title"),
        config.get("searching_description"),
        int(config.get("embed_color"), 16)
    )

    # 중복 제거 + 최대 20곡 제한
    valid_song_list = []
    for song_url in song_list:
        if song_url not in music_state.search_cache:
            valid_song_list.append({'url': song_url})
            music_state.search_cache.add(song_url)
        if len(valid_song_list) >= 20:
            break

    # 캐시 크기 제한 초과 시 초기화
    if len(music_state.search_cache) > 1000:
        music_state.search_cache.clear()

    # 음성 채널 연결 확인
    if not await ensure_voice_channel_connection(message.author, music_state):
        if processing_embed_message:
            await processing_embed_message.delete()
        return

    # 병렬 작업 준비 및 실행
    tasks = []
    for song in valid_song_list:
        try:
            task = asyncio.create_task(process_and_add_songs([song], message))
            music_state.add_search_task(task)
            tasks.append(task)
            await asyncio.sleep(0.8)  # 디도스 방지용
        except Exception as e:
            print(f"[ERROR] 곡 추가 중 오류 발생: {e}, 곡 URL: {song['url']}")

    # 모든 병렬 작업 완료 대기
    await asyncio.gather(*tasks)
    print(f"[INFO] {len(valid_song_list)}개의 곡이 대기열에 추가되었습니다.")

    # 재생 트리거 (전부 추가된 후에만)
    if music_state.queue and not music_state.is_playing:
        try:
            print("[INFO] 모든 곡 추가 완료. 첫 곡 재생 시작.")
            play_next_task = asyncio.create_task(play_next_song(message.channel, music_state))
            music_state.add_search_task(play_next_task)
            await play_next_task
        except Exception as e:
            print(f"[ERROR] 첫 곡 재생 중 오류: {e}")

    # 임베드 메시지 제거
    if processing_embed_message:
        await processing_embed_message.delete()

    # 취소 조건 시 자원 해제
    if music_state.some_condition_to_cancel_task:
        await music_state.close()
        print("[INFO] 병렬 작업이 종료되었습니다.")



#유튜브 재생목록 전용 플레이리스트 정보 분석/추출 함수
async def fetch_playlist_data(url, api_key, message):
    """유튜브 재생목록 내 곡들의 URL만 추출"""
    try:
        playlist_id = extract_playlist_id(url)
        if not playlist_id:
            raise ValueError("재생목록 ID를 추출하지 못했습니다.")

        print(f"[DEBUG] 추출된 재생목록 ID: {playlist_id}")

        # 유튜브 API 요청 URL
        api_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={playlist_id}&maxResults=50&key={api_key}"

        # aiohttp 세션 관리
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(api_url) as response:
                    if response.status != 200:
                        raise Exception(f"유튜브 API 오류: {response.status} - {await response.text()}")

                    # JSON 데이터 파싱
                    data = await response.json()
                    if "items" not in data:
                        raise Exception("재생목록 데이터를 가져오지 못했어요. 'items' 항목이 없습니다.")

                    # URL 리스트 생성
                    song_info = [
                        {
                            "url": f"https://www.youtube.com/watch?v={item['snippet']['resourceId']['videoId']}"
                        }
                        for item in data["items"]
                    ]
                    print(f"[DEBUG] 추출된 URL 리스트: {song_info}")

                    # 패러렐 함수로 넘기기
                    if song_info:
                        await fetch_songs_in_parallel([song["url"] for song in song_info], message)

                    return song_info  # URL 리스트 반환

            except Exception as e:
                print(f"[ERROR] 유튜브 API 요청 중 오류 발생: {e}")
                return None

    except Exception as e:
        print(f"[ERROR] 재생목록 정보 추출 실패: {e}")
        return None

#빠르게 추가되어서 대기열에 중복으로 추가된 곡들을 제거하고 깔끔하게 대기열을 정리하는 함수
async def remove_duplicates_from_queue():
    """대기열에서 중복 항목 제거"""
    if not music_state.queue:
        print("[ERROR] 대기열이 비어 있습니다.")
        return

    seen = set()
    unique_queue = []
    for song in music_state.queue:
        if song['url'] not in seen:  # URL을 기준으로 중복 체크
            unique_queue.append(song)
            seen.add(song['url'])

    music_state.queue = unique_queue
    print(f"[INFO] 대기열 중복 제거 완료. 현재 대기열: {len(music_state.queue)}곡 남음.")

#재생목록과 일반 영상의 링크가 합쳐진 특수한 재생목록 링크에서 재생목록 데이터를 추출하고 재생목록을 검색하게 해주는 함수
def extract_playlist_id(playlist_url):
    """재생목록 URL에서 playlistId 추출"""
    try:
        parsed_url = urllib.parse.urlparse(playlist_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        return query_params.get("list", [None])[0]
    except Exception as e:
        print(f"[ERROR] 재생목록 ID 추출 실패: {e}")
        return None

#대기열의 곡을 재생하고 임베드 갱신 함수를 곡이 넘어갈때마다 호출해서 새로운 곡에 맞는 새로운 임베드와 함께 재생하게 해주는 메인 함수
# 🎵 다음 곡 재생 또는 종료 상태 전환
async def play_next_song(channel, music_state):
    """🎵 다음 곡을 재생하거나, 종료 상태로 전환"""

    # 반복 모드 처리
    if music_state.repeat and music_state.current_song:
        if not music_state.queue or music_state.queue[0] != music_state.current_song:
            music_state.queue.insert(0, music_state.current_song)
            print(f"🔁 반복 모드: '{music_state.current_song['title']}'를 대기열 맨 앞에 추가했습니다.")

    if not music_state.queue:
        print("✅ 재생할 다음 곡이 없습니다. 상태 초기화 시작.")

        # 상태 초기화
        music_state.current_song = None
        music_state.is_playing = False
        music_state.is_paused = False
        music_state.current_duration = 0
        music_state.current_start_time = 0
        music_state.last_elapsed_time = 0

        # 설정에서 채널명 읽기
        config = load_config("설정.txt")
        channel_name = config.get("CHANNEL_NAME", "")
        embed_title = config.get("currently_playing_embed_title", "")

        # 임베드 삭제
        if channel.guild:
            target_channel = discord.utils.get(channel.guild.text_channels, name=channel_name)
            if target_channel:
                try:
                    async for msg in target_channel.history(limit=50):
                        if msg.embeds and embed_title in msg.embeds[0].title:
                            await clean_channel_message(target_channel)  # 이걸로 충분
                            print(f"🧹 '{embed_title}' 임베드를 삭제했습니다.")
                            break
                except Exception as e:
                    print(f"[ERROR] 임베드 삭제 실패: {e}")

        # 음성 채널에서 자동 퇴장
        if music_state.voice_client and music_state.voice_client.is_connected():
            try:
                await music_state.voice_client.disconnect()
                music_state.voice_client = None
                print("👋 음성 채널에서 자동으로 퇴장했습니다.")
            except Exception as e:
                print(f"[ERROR] 자동 퇴장 중 오류 발생: {e}")

        music_state.is_playing_next_song = False
        return

    next_song = music_state.queue.pop(0)
    if not next_song:
        print(f"[ERROR] 대기열에서 유효한 곡을 가져오지 못했습니다.")
        music_state.is_playing_next_song = False
        return

    print(f"[INFO] '{next_song['title']}' 곡을 재생합니다.")
    music_state.current_song = next_song

    try:
        audio_url, duration = await extract_audio_url(next_song["url"])
        if not audio_url:
            raise RuntimeError(f"[ERROR] '{next_song['title']}'의 오디오 URL을 가져오지 못했습니다.")

        music_state.current_duration = duration
        music_state.current_start_time = time.time()
        music_state.last_elapsed_time = 0

        if not music_state.voice_client or not music_state.voice_client.is_connected():
            if music_state.voice_channel:
                music_state.voice_client = await music_state.voice_channel.connect(reconnect=True)
            else:
                print("[ERROR] 음성 채널이 설정되지 않아 재생할 수 없습니다.")
                music_state.is_playing_next_song = False
                return

        if music_state.voice_client.is_playing():
            music_state.voice_client.stop()

        source = discord.FFmpegPCMAudio(
            next_song["url"] if next_song.get("is_local") else audio_url, **ffmpeg_opts
        )

        def after_playback(error):
            if error:
                print(f"[ERROR] 재생 중 오류: {error}")
            future = asyncio.run_coroutine_threadsafe(
                play_next_song(channel, music_state), bot.loop
            )
            try:
                future.result()
            except Exception as e:
                print(f"[ERROR] after_playback 실패: {e}")

        music_state.voice_client.play(source, after=after_playback)

        music_state.is_playing = True
        music_state.is_paused = False

        if not update_embed_timer.is_running():
            asyncio.run_coroutine_threadsafe(update_embed_timer.start(channel, music_state), bot.loop)

        await update_playing_embed(
            music_state, channel, next_song["title"], next_song["url"],
            next_song.get("thumbnail_url"), elapsed_time=0, total_duration=duration
        )

    except Exception as e:
        print(f"[ERROR] 곡 재생 중 오류 발생: {repr(e)}")

    finally:
        music_state.is_playing_next_song = False


#여기부터
@tasks.loop(seconds=3)
async def update_embed_timer(channel, music_state):
    if music_state.is_playing or music_state.is_paused:
        if music_state.is_searching:  # 검색 중인 경우, 타이머 일시정지
            if music_state.search_pause_time is None:
                music_state.search_pause_time = time.time()
            return  # 일시 정지 상태에서는 타이머를 진행하지 않음

        if music_state.is_playing:
            elapsed_time = time.time() - music_state.current_start_time  # 음악이 재생 중일 때
        else:
            elapsed_time = music_state.elapsed_paused_time  # 일시 정지된 상태에서는 멈춘 시간 사용

        # 진행 시간과 총 길이를 계산하여 임베드 갱신
        await update_playing_embed(
            music_state,
            channel,
            music_state.current_song["title"],
            music_state.current_song["url"],
            music_state.current_song.get("thumbnail_url"),
            elapsed_time,
            music_state.current_duration
        )
    else:
        if update_embed_timer.is_running():
            update_embed_timer.stop()  # 타이머가 실행 중일 때만 멈추게 함

async def start_embed_timer(channel, music_state):
    """타이머 시작 시 메인 루프에서 실행되도록 보장"""
    if not update_embed_timer.is_running():
        # 검색이 시작되면, 이전 진행 시간을 저장하고 타이머를 재개
        if music_state.is_searching and music_state.search_pause_time:
            paused_time = time.time() - music_state.search_pause_time
            music_state.current_start_time += paused_time  # 검색된 시간만큼 진행 시간 점프

            # 검색 후, 다시 일시정지된 시간을 초기화
            music_state.search_pause_time = None

        # asyncio.create_task를 사용해 비동기적으로 타이머 시작
        asyncio.create_task(update_embed_timer.start(channel, music_state))

async def extract_audio_url(video_url):
    """YouTube URL 또는 로컬 파일에서 오디오 스트림 URL 및 길이를 추출"""
    if os.path.exists(video_url):  # ✅ 로컬 파일이면 직접 경로 반환
        try:
            audio = MP3(video_url)
            duration = int(audio.info.length)  # 초 단위 변환
            return video_url, duration  # 로컬 파일 경로 반환
        except Exception as e:
            print(f"[ERROR] 로컬 파일 재생 시간 추출 실패: {e}")
            return None, 0

    # ✅ YouTube URL 처리
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,  # 출력 최소화
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.cache.remove()
            print("[INFO] yt-dlp 캐시 제거 성공!")

            info = ydl.extract_info(video_url, download=False)
            audio_url = info["url"]
            duration = info.get("duration", 0)
            return audio_url, duration
    except Exception as e:
        print(f"[ERROR] 오디오 스트림 URL 추출 실패: {e}")
        return None, 0

def create_progress_bar(elapsed_time, total_duration, width=21):
    progress = elapsed_time / total_duration if total_duration > 0 else 0
    if progress >= 1:
        filled_length = width - 1
    else:
        filled_length = int(min(progress * (width - 1), width - 1))

    empty_length = width - filled_length - 1
    progress_bar = "🟩" * filled_length + "🟢" + "⬜" * empty_length
    return progress_bar



async def resize_gif(file_bytes):
    try:
        with Image.open(io.BytesIO(file_bytes)) as im:
            frames = []
            for frame in ImageSequence.Iterator(im):
                frame = frame.convert("RGBA")
                frame = frame.resize((128, 128), Image.LANCZOS)
                frames.append(frame)

            output = io.BytesIO()
            frames[0].save(
                output,
                format='GIF',
                save_all=True,
                append_images=frames[1:],
                loop=0,
                disposal=2,
                optimize=False
            )
            output.seek(0)
            return output
    except Exception as e:
        print(f"[ERROR] GIF 리사이즈 실패: {e}")
        return None
    
def convert_gif_to_mid_frame_jpg(gif_bytes_io):
    try:
        with Image.open(gif_bytes_io) as img:
            mid_frame = img.n_frames // 2
            img.seek(mid_frame)
            img = img.convert("RGB")
            img = img.resize((128, 128), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            buf.seek(0)
            return buf
    except Exception as e:
        print(f"[ERROR] GIF 프레임 추출 실패: {e}")
        return None


async def resize_to_emoji_size(file_bytes, ext):
    try:
        with Image.open(io.BytesIO(file_bytes)) as img:
            img = img.convert("RGBA")
            img = img.resize((128, 128), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG" if ext.lower() != "jpg" else "JPEG")
            buf.seek(0)
            return buf
    except Exception as e:
        print(f"[ERROR] 이미지 리사이즈 실패: {e}")
        return None

async def download_file(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                return await resp.read() if resp.status == 200 else None
    except Exception as e:
        print(f"[ERROR] 파일 다운로드 실패: {e}")
        return None

def load_emoji_json():
    if not os.path.exists(EMOJI_JSON_PATH):
        return {}
    with open(EMOJI_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_emoji_json(data):
    with open(EMOJI_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@tasks.loop(seconds=3)
async def update_time(channel, music_state):
    if not music_state.is_playing or not music_state.current_song:
        update_time.stop()
        return

    try:
        elapsed_time = int(time.time() - music_state.current_start_time)

        if music_state.last_played_embed:
            config = load_config("설정.txt")  # 대사 파일 로드
            embed_color = int(config.get('embed_color'), 16)  # 색상 코드 호출
            embed_title = config.get('currently_playing_embed_title')  # 제목 호출

            await music_state.last_played_embed.edit(embed=create_embed(
                embed_title,
                f"[{music_state.current_song['title']}]({music_state.current_song['url']})",
                f"진행 시간: {elapsed_time // 60}:{elapsed_time % 60:02}",
                color=embed_color
            ))
    except Exception as e:
        print(f"[ERROR] 타이머 갱신 중 오류 발생: {e}")
        update_time.stop()
#여기까지는 그냥 타이머와 그에 따라서 갱신되는 재생 임베드의 재생바 관련 함수




#play_next_song에서 호출되어서 곡이 넘어갈때마다 알맞은 곡 정보의 임베드로 갱신을 하게 해주는 함수
async def update_playing_embed(music_state, channel, title, url, image_url, elapsed_time, total_duration):
    async with music_state.embed_lock:  # 락으로 동기화
        # 시간 계산
        elapsed_minutes, elapsed_seconds = divmod(int(elapsed_time), 60)
        total_minutes, total_seconds = divmod(int(total_duration), 60)
        elapsed_str = f"{elapsed_minutes:02}:{elapsed_seconds:02}"
        total_str = f"{total_minutes:02}:{total_seconds:02}"

        # 진행바 이미지 생성
        progress_bar = create_progress_bar(elapsed_time, total_duration)

        # 대사 파일에서 색상 코드 및 타이틀 가져오기
        config = load_config("설정.txt")
        embed_color = int(config.get('embed_color'), 16)  # 색상 코드 호출
        embed_title = config.get('currently_playing_embed_title')  # 타이틀 호출

        embed = discord.Embed(
            title=embed_title,
            description=f"[{title}]({url})",
            color=embed_color
        )

        if image_url:
            embed.set_image(url=image_url)

        # 진행바와 전체 곡 시간만 표시
        embed.set_footer(text=f"{progress_bar} [{total_str}]")

        try:
            if music_state.is_embed_active and music_state.last_played_embed:
                # 기존 임베드가 있으면 수정
                await music_state.last_played_embed.edit(embed=embed)
            else:
                # 임베드가 없으면 새로 생성
                music_state.last_played_embed = await channel.send(embed=embed)
                music_state.is_embed_active = True
        except Exception as e:
            print(f"[ERROR] 임베드 갱신 중 오류 발생: {e}")
            # 임베드 갱신이 실패하면 새로 생성
            music_state.last_played_embed = await channel.send(embed=embed)
            music_state.is_embed_active = True

def create_embed(title: str, description: str = "", color: int = None) -> discord.Embed:
    config = load_config("설정.txt")  # 설정 파일에서 기본 색상 불러오기
    embed_color = config.get('embed_color')

    # 색상 처리
    if color is None and embed_color:
        try:
            color = int(embed_color, 16)
        except ValueError:
            color = None  # 잘못된 색상 코드면 None 처리

    # title과 description이 숫자형일 경우 문자열로 변환
    if not isinstance(title, str):
        title = str(title)
    if not isinstance(description, str):
        description = str(description)

    # title이 숫자만 있을 경우 무의미한 숫자 방지 처리
    if title.isdigit():
        title = "제목 없음"

    # description이 숫자만 있을 경우 무시
    if description.isdigit():
        description = ""

    return discord.Embed(title=title, description=description, color=color)







@bot.command()
async def 설정(ctx):
    await ctx.message.delete()
    guild = ctx.guild
    bot_member = guild.me

    config = load_config("설정.txt")
    channel_name = config.get("CHANNEL_NAME")
    control_panel_title = config.get("control_panel_title")
    temp_channel_keyword = config.get("TEMP_CHANNEL_NAME")
    emoji_store_channel_name = config.get("emoji_store_channel")
    emoji_panel_channel_name = config.get("EMOJI_CHANNEL")
    reactions = ["⏹️", "🔁", "⏯️", "⏭️", "🔀", "🗒️", "⏱️", "❓"]

    # === 권한 로직 ===
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False, connect=False, speak=False, send_messages=False
        )
    }

    real_roles = [
        role for role in guild.roles
        if not role.is_bot_managed() and role != bot_member.top_role and role != guild.default_role
    ]

    if real_roles:
        for role in real_roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, send_messages=True
            )
    else:
        overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, send_messages=True
        )

    # === TEMP 카테고리 생성 또는 수정 ===
    category = discord.utils.get(guild.categories, name=TEMP_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(TEMP_CATEGORY_NAME, overwrites=overwrites)
        print(f"[INFO] 카테고리 '{TEMP_CATEGORY_NAME}' 생성됨.")
    else:
        if category.overwrites != overwrites:
            await category.edit(overwrites=overwrites)
            print(f"[SYNC] 카테고리 '{TEMP_CATEGORY_NAME}' 권한 동기화됨.")

    # === 트리거 음성 채널 생성 또는 수정 ===
    trigger_channel = discord.utils.get(guild.voice_channels, name=TRIGGER_CHANNEL_NAME)
    if not trigger_channel:
        await guild.create_voice_channel(TRIGGER_CHANNEL_NAME, category=category, overwrites=overwrites)
        print(f"[INFO] 트리거 채널 '{TRIGGER_CHANNEL_NAME}' 생성됨.")
    else:
        if trigger_channel.overwrites != overwrites:
            await trigger_channel.edit(overwrites=overwrites)
            print(f"[SYNC] 트리거 채널 권한 동기화됨.")

    # === 음악 텍스트 채널 생성 또는 수정 ===
    music_channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not music_channel:
        try:
            music_channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)
            print(f"[INFO] '{channel_name}' 텍스트 채널 생성됨.")
        except discord.errors.HTTPException:
            fallback_name = re.sub(r"[^\wㄱ-ㅎ가-힣0-9]", "", channel_name)
            music_channel = await guild.create_text_channel(name=fallback_name, category=category, overwrites=overwrites)
            print(f"[INFO] '{fallback_name}' 텍스트 채널 생성됨.")
    else:
        if music_channel.category != category:
            await music_channel.edit(category=category)
            print(f"[SYNC] '{channel_name}' 채널 카테고리 이동됨.")
        if music_channel.overwrites != overwrites:
            await music_channel.edit(overwrites=overwrites)
            print(f"[SYNC] '{channel_name}' 텍스트 채널 권한 동기화됨.")

    # === 컨트롤 패널 메시지 생성 또는 유지 ===
    control_panel_embed = create_embed(control_panel_title)
    control_message = None
    async for message in music_channel.history(limit=10):
        if message.embeds:
            for embed in message.embeds:
                if embed.title == control_panel_title:
                    control_message = message
                    break
        if control_message:
            break

    if not control_message:
        control_message = await music_channel.send(embed=control_panel_embed)
        for r in reactions:
            try:
                await control_message.add_reaction(r)
            except:
                continue

    data = {
        f"{channel_name}_control_panel_message_id": control_message.id
    }
    save_channel_data(guild.id, data)

    # === 이모지 저장 채널 처리 ===
    if emoji_store_channel_name:
        emoji_store_channel = discord.utils.get(guild.text_channels, name=emoji_store_channel_name)
        if not emoji_store_channel:
            await guild.create_text_channel(emoji_store_channel_name, overwrites=overwrites)
            print(f"[INFO] '{emoji_store_channel_name}' 채널 생성됨.")
        else:
            if emoji_store_channel.overwrites != overwrites:
                await emoji_store_channel.edit(overwrites=overwrites)
                print(f"[SYNC] '{emoji_store_channel_name}' 채널 권한 동기화됨.")







async def read_guide_text(file_path):
    try:
        async with aiofiles.open(file_path, mode='r', encoding='utf-8') as f:
            content = await f.read()
            return content
    except FileNotFoundError:
        return "❗ 가이드 파일을 찾을 수 없습니다. 관리자에게 문의해주세요."

async def send_help_dm(user):
    # 가이드 설명 불러오기
    commands_description = await read_guide_text("가이드.txt")

    # 색상코드 불러오기
    config = load_config("설정.txt")
    embed_color = int(config.get('embed_color'), 16)

    # 임베드 생성
    embed = discord.Embed(
        title="📘 커스텀 디스코드 봇 가이드",
        description=commands_description,
        color=embed_color
    )
    embed.set_footer(text="문의: 디스코드 ready22 | 이메일: wakamoli1213@gmail.com")

    # DM 전송
    await user.send(embed=embed)










# 데이터 저장 함수들
def save_channel_data(guild_id, data):
    """서버에 대한 채널 데이터를 저장하는 함수"""
    file_path = f"data/{guild_id}_channel_data.json"
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def load_channel_data(guild_id):
    """서버에 대한 채널 데이터를 불러오는 함수"""
    file_path = f"data/{guild_id}_channel_data.json"
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}





def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return dict(line.strip().split("=", 1) for line in f if "=" in line)



def calculate_hash(byte_data):
    return hashlib.sha256(byte_data).hexdigest()

missing_access_guilds = set()

async def monitor_emoji_channel_loop(bot):
    await bot.wait_until_ready()
    config = load_config("설정.txt")
    channel_name = config.get("emoji_store_channel")

    while not bot.is_closed():
        for guild in bot.guilds:
            if guild.id in missing_access_guilds:
                continue  # 권한 없는 서버는 무시

            emoji_channel = discord.utils.get(guild.text_channels, name=channel_name)
            if not emoji_channel:
                continue

            try:
                emoji_data = load_emoji_json()
                existing_emoji_names = {e.name for e in guild.emojis}
                current_names = set()
                updated_data = {}

                async for msg in emoji_channel.history(limit=100, oldest_first=True):
                    # ✅ 봇이 보낸 메시지일 경우 이름만 추출해서 current_names에는 포함
                    if msg.author == bot.user:
                        if msg.attachments:
                            filename = msg.attachments[0].filename
                            name, _ = os.path.splitext(filename)
                            current_names.add(name.strip().lower())
                        continue

                    if not msg.attachments:
                        continue

                    attachment = msg.attachments[0]
                    filename = attachment.filename
                    name, ext = os.path.splitext(filename)
                    name = name.strip().lower()
                    ext = ext[1:].lower()

                    if ext not in ["png", "jpg", "jpeg", "gif"]:
                        continue

                    current_names.add(name)

                    # ✅ 기존 URL과 동일하면 skip
                    if name in emoji_data and emoji_data[name] == attachment.url:
                        updated_data[name] = emoji_data[name]
                        continue

                    # ✅ 이미 등록된 이모지와 이름이 겹치면 URL만 업데이트
                    if name in existing_emoji_names:
                        updated_data[name] = attachment.url
                        continue

                    file_bytes = await download_file(attachment.url)
                    if not file_bytes:
                        continue

                    try:
                        if ext == "gif":
                            resized_gif_io = await resize_gif(file_bytes)
                            if not resized_gif_io:
                                continue
                            resized_gif_io.seek(0)

                            uploaded_msg = await emoji_channel.send(
                                file=discord.File(resized_gif_io, filename=f"{name}.gif"))
                            uploaded_url = uploaded_msg.attachments[0].url

                            jpg_io = convert_gif_to_mid_frame_jpg(io.BytesIO(file_bytes))
                            if not jpg_io:
                                continue
                            jpg_io.seek(0)

                            emoji = await guild.create_custom_emoji(name=name, image=jpg_io.read())

                        else:
                            resized_io = await resize_to_emoji_size(file_bytes, ext)
                            if not resized_io:
                                continue
                            resized_io.seek(0)

                            uploaded_msg = await emoji_channel.send(
                                file=discord.File(resized_io, filename=f"{name}.{ext}"))
                            uploaded_url = uploaded_msg.attachments[0].url

                            emoji = await guild.create_custom_emoji(name=name, image=resized_io.read())

                        updated_data[emoji.name] = uploaded_url
                        print(f"[INFO] 이모지 등록: {emoji.name}")
                        await asyncio.sleep(2.5)

                        try:
                            await msg.delete()
                        except:
                            print(f"[WARNING] 메시지 삭제 실패: {name}")

                    except discord.Forbidden:
                        print(f"[WARNING] 권한 없음 - 서버 무시: {guild.name}")
                        missing_access_guilds.add(guild.id)
                        break
                    except Exception as e:
                        print(f"[ERROR] 이모지 등록 실패 ({name}): {e}")
                        continue

                # ✅ 현재 메시지에 없는 이모지만 삭제
                for emoji in guild.emojis:
                    if emoji.name not in current_names:
                        try:
                            await emoji.delete()
                            print(f"[INFO] 삭제된 이모지: {emoji.name}")
                            # ❗ JSON에서도 제거
                            updated_data.pop(emoji.name, None)
                        except discord.Forbidden:
                            print(f"[WARNING] 이모지 삭제 권한 없음 또는 이미 삭제됨: {emoji.name}")
                        except:
                            pass

                # ✅ 기존 이모지 정보와 병합하여 저장
                merged_data = {**emoji_data, **updated_data}
                save_emoji_json(merged_data)

            except discord.Forbidden:
                print(f"[WARNING] Missing Access - 서버 무시: {guild.name}")
                missing_access_guilds.add(guild.id)
                continue
            except Exception as e:
                print(f"[ERROR] 메시지 처리 중 오류: {e}")
                continue

        await asyncio.sleep(5)

@bot.command()
async def 서버(ctx):
    await ctx.message.delete()
    guilds = bot.guilds
    msg = "**봇이 참가한 서버 목록:**\n"
    for guild in guilds:
        msg += f"- {guild.name} (ID: {guild.id})\n"
    await ctx.send(msg)


@bot.command()
@commands.is_owner()  # 봇 소유자만 사용 가능하도록 제한
async def 탈출(ctx):
    await ctx.message.delete()
    config = load_config("설정.txt")
    allowed_names = [name.strip() for name in config.get("allow_server", "").split(",")]

    left_servers = []

    for guild in bot.guilds:
        if guild.name not in allowed_names:
            try:
                await guild.leave()
                left_servers.append(guild.name)
            except discord.Forbidden:
                await ctx.send(f"❌ `{guild.name}` 서버에서 나갈 권한이 없음.")
            except discord.HTTPException:
                await ctx.send(f"⚠️ `{guild.name}` 서버에서 나가는데 실패했음.")

    if left_servers:
        await ctx.send(f"✅ 다음 서버에서 탈출 완료: {', '.join(left_servers)}")
    else:
        await ctx.send("✅ 나갈 서버가 없습니다.")






async def leave_unallowed_servers():
    config = load_config("설정.txt")
    allowed_names = [name.strip() for name in config.get("allow_server", "").split(",")]

    left_servers = []

    for guild in bot.guilds:
        if guild.name not in allowed_names:
            try:
                await guild.leave()
                left_servers.append(guild.name)
            except discord.Forbidden:
                print(f"❌ `{guild.name}` 서버에서 나갈 권한이 없음.")
            except discord.HTTPException:
                print(f"⚠️ `{guild.name}` 서버에서 나가는데 실패했음.")

    return left_servers




@bot.event
async def on_ready():
    config = load_config("설정.txt")
    channel_name = config.get("CHANNEL_NAME")
    control_panel_title = config.get("control_panel_title")
    status_message = config.get("status")
    temp_channel_keyword = config.get("TEMP_CHANNEL_NAME")
    emoji_store_channel_name = config.get("emoji_store_channel")
    emoji_panel_channel_name = config.get("EMOJI_CHANNEL")
    left_servers = await leave_unallowed_servers()

    if left_servers:
        print(f"✅ 다음 서버에서 탈출 완료: {', '.join(left_servers)}")
    await update_yt_dlpp()
    bot.loop.create_task(monitor_emoji_channel_loop(bot))

    if not periodic_leave_task.is_running():
        periodic_leave_task.start()

    await cache_role_embed_messages(bot)

    # 재생 기록 초기화
    if not os.path.exists(history_file_path):
        try:
            with open(history_file_path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            print(f"[INFO] '{history_file_path}' 생성 완료")
        except Exception as e:
            print(f"[ERROR] '{history_file_path}' 생성 실패: {e}")
    else:
        try:
            with open(history_file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    recent_tracks.extend(loaded)
        except Exception as e:
            print(f"[ERROR] '{history_file_path}' 로드 실패: {e}")

    for guild in bot.guilds:
        try:
            bot_member = guild.me
            if not bot_member.guild_permissions.manage_channels:
                print(f"[WARN] {guild.name}: 채널 생성 권한 없음")
                continue

            # TEMP 카테고리 및 트리거 채널 생성
            try:
                category = await create_temp_category_and_channel(guild)
            except discord.Forbidden:
                print(f"[WARN] '{guild.name}' TEMP 채널 생성 권한 없음")
                category = None
            except Exception as e:
                print(f"[ERROR] TEMP 채널 처리 오류: {e}")
                category = None

            # TEMP 채널 정리
            if temp_channel_keyword:
                for vc in guild.voice_channels:
                    if temp_channel_keyword in vc.name and not vc.members:
                        try:
                            await vc.delete(reason="봇 시작 시 자동 정리 (TEMP 채널)")
                        except discord.Forbidden:
                            print(f"[WARN] TEMP 채널 삭제 권한 없음: {vc.name}")
                        except Exception as e:
                            print(f"[ERROR] TEMP 채널 삭제 오류: {e}")

            # 권한 구성
            overwrites = await get_proper_overwrites(guild)

            # 이모지 저장 채널 처리
            if emoji_store_channel_name:
                try:
                    emoji_store_channel = discord.utils.get(guild.text_channels, name=emoji_store_channel_name)
                    if not emoji_store_channel:
                        await guild.create_text_channel(emoji_store_channel_name, overwrites=overwrites, category=category)
                        print(f"[INFO] '{emoji_store_channel_name}' 채널 생성됨")
                    else:
                        if emoji_store_channel.overwrites != overwrites:
                            await emoji_store_channel.edit(overwrites=overwrites)
                            print(f"[SYNC] '{emoji_store_channel_name}' 권한 동기화됨")
                except discord.Forbidden:
                    print(f"[WARN] '{emoji_store_channel_name}' 채널 권한 부족")
                except Exception as e:
                    print(f"[ERROR] 이모지 저장 채널 처리 오류: {e}")

            # 음악 텍스트 채널 처리
            try:
                music_channel = discord.utils.get(guild.text_channels, name=channel_name)
                if not music_channel:
                    music_channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites, category=category)
                    print(f"[INFO] '{channel_name}' 채널 생성됨")
                else:
                    if category and music_channel.category != category:
                        await music_channel.edit(category=category)
                        print(f"[SYNC] '{channel_name}' 채널을 카테고리로 이동")
                    if music_channel.overwrites != overwrites:
                        await music_channel.edit(overwrites=overwrites)
                        print(f"[SYNC] '{channel_name}' 권한 동기화됨")
            except discord.Forbidden:
                print(f"[WARN] '{channel_name}' 채널 생성/수정 권한 부족")
                continue
            except Exception as e:
                print(f"[ERROR] 음악 채널 처리 오류: {e}")
                continue

            music_state.cached_channel_id = music_channel.id

            # 컨트롤 패널 메시지 처리
            try:
                control_panel_embed = create_embed(control_panel_title)
                control_message = None
                async for message in music_channel.history(limit=10):
                    if message.embeds and any(embed.title == control_panel_title for embed in message.embeds):
                        control_message = message
                        break
                if not control_message:
                    control_message = await music_channel.send(embed=control_panel_embed)

                required_reactions = ["⏹️", "🔁", "⏯️", "⏭️", "🔀", "🗒️", "⏱️", "❓"]
                existing = set()
                for r in control_message.reactions:
                    try:
                        if r.emoji in required_reactions:
                            existing.add(r.emoji)
                    except:
                        continue
                for emoji in [e for e in required_reactions if e not in existing]:
                    try:
                        await control_message.add_reaction(emoji)
                    except:
                        continue
            except discord.Forbidden:
                print(f"[WARN] 컨트롤 패널 메시지 권한 부족 in {guild.name}")
            except Exception as e:
                print(f"[ERROR] 컨트롤 패널 메시지 처리 오류: {e}")

            # 메시지 정리 및 패널 캐싱
            try:
                await clean_channel_message(music_channel)
                await cache_control_panel_message(music_channel)
            except Exception as e:
                print(f"[ERROR] 채널 메시지 정리 또는 캐싱 실패: {e}")

            # 이모지 패널 반응 동기화
            try:
                data = load_channel_data(guild.id)
                emoji_msg_id = data.get("emoji_message_id")
                if emoji_msg_id and emoji_panel_channel_name:
                    emoji_channel = discord.utils.get(guild.text_channels, name=emoji_panel_channel_name)
                    if emoji_channel:
                        emoji_msg = await emoji_channel.fetch_message(emoji_msg_id)
                        existing_emoji_names = {
                            r.emoji.name for r in emoji_msg.reactions if isinstance(r.emoji, discord.Emoji)
                        }
                        files = [f for f in os.listdir(EMOJI_FOLDER) if f.lower().endswith(('.png', '.jpg', '.gif'))]
                        emoji_names = [os.path.splitext(f)[0] for f in files]
                        for name in emoji_names:
                            if name not in existing_emoji_names:
                                emoji_obj = discord.utils.get(guild.emojis, name=name)
                                if emoji_obj:
                                    try:
                                        await emoji_msg.add_reaction(emoji_obj)
                                        await asyncio.sleep(0.5)
                                    except Exception as e:
                                        print(f"[WARN] '{name}' 반응 추가 실패: {e}")
            except discord.Forbidden:
                print(f"[WARN] 이모지 패널 접근 권한 부족: {guild.name}")
            except Exception as e:
                print(f"[ERROR] 이모지 패널 처리 중 오류: {e}")

        except Exception as e:
            print(f"[ERROR] '{guild.name}' 전체 처리 중 오류: {e}")
            continue

    if status_message:
        try:
            await bot.change_presence(activity=discord.Game(name=status_message))
        except Exception as e:
            print(f"[ERROR] 상태 메시지 설정 실패: {e}")



@tasks.loop(minutes=10)
async def periodic_leave_task():
    left = await leave_unallowed_servers()
    if left:
        print(f"✅ 주기적 탈출 완료: {', '.join(left)}")

async def get_proper_overwrites(guild):
    bot_member = guild.me
    roles = [
        role for role in guild.roles
        if not role.is_bot_managed() and role != bot_member.top_role and role != guild.default_role
    ]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False, send_messages=False, connect=False, speak=False
        )
    }

    if roles:
        for role in roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, connect=True, speak=True
            )
    else:
        overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, connect=True, speak=True
        )

    return overwrites


async def create_temp_category_and_channel(guild):
    overwrites = await get_proper_overwrites(guild)

    category = discord.utils.get(guild.categories, name=TEMP_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(TEMP_CATEGORY_NAME, overwrites=overwrites)
        print(f"[INFO] 카테고리 '{TEMP_CATEGORY_NAME}' 생성됨.")
    else:
        if category.overwrites != overwrites:
            await category.edit(overwrites=overwrites)
            print(f"[SYNC] 카테고리 '{TEMP_CATEGORY_NAME}' 권한 동기화됨.")

    trigger_channel = discord.utils.get(guild.voice_channels, name=TRIGGER_CHANNEL_NAME)
    if not trigger_channel:
        await guild.create_voice_channel(TRIGGER_CHANNEL_NAME, category=category, overwrites=overwrites)
        print(f"[INFO] 트리거 채널 '{TRIGGER_CHANNEL_NAME}' 생성됨.")
    else:
        if trigger_channel.category != category:
            await trigger_channel.edit(category=category)
            print(f"[SYNC] 트리거 채널 카테고리 이동")
        if trigger_channel.overwrites != overwrites:
            await trigger_channel.edit(overwrites=overwrites)
            print(f"[SYNC] 트리거 채널 권한 동기화됨.")

    return category


async def auto_register(guild):
    config = load_config("설정.txt")
    channel_name = config.get("CHANNEL_NAME")
    control_panel_title = config.get("control_panel_title")
    overwrites = await get_proper_overwrites(guild)

    existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
    if existing_channel:
        if existing_channel.overwrites != overwrites:
            await existing_channel.edit(overwrites=overwrites)
            print(f"[SYNC] '{channel_name}' 채널 권한 동기화됨.")
        return

    try:
        channel = await guild.create_text_channel(channel_name, overwrites=overwrites)
        print(f"[INFO] 텍스트 채널 '{channel_name}' 생성됨.")
    except discord.errors.HTTPException:
        fallback_name = re.sub(r"[^\wㄱ-ㅎ가-힣0-9]", "", channel_name)
        channel = await guild.create_text_channel(fallback_name, overwrites=overwrites)
        print(f"[INFO] 예비 채널 이름 '{fallback_name}' 생성됨.")

    control_panel_embed = create_embed(control_panel_title)
    control_message = await channel.send(embed=control_panel_embed)

    for reaction in ["⏹️", "🔁", "⏯️", "⏭️", "🔀", "🗒️", "⏱️", "❓"]:
        try:
            await control_message.add_reaction(reaction)
        except:
            continue

    data = {f"{channel_name}_control_panel_message_id": control_message.id}
    save_channel_data(guild.id, data)



async def safe_delete_message(message):
    """메시지를 안전하게 삭제"""
    try:
        await message.delete()
    except discord.errors.NotFound:
        print(f"[INFO] 메시지가 이미 삭제되었습니다: {message.id}")
    except Exception as e:
        print(f"[ERROR] 메시지 삭제 중 오류 발생: {e}")

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW

async def update_yt_dlpp():
    try:
        process = await asyncio.create_subprocess_exec(
            'python', '-m', 'pip', 'install', '--upgrade', 'yt-dlp',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW  # <-- 이게 핵심
        )
        await process.communicate()
    except Exception:
        pass

async def clean_channel_message(channel):
    """채널 메시지 정리 (컨트롤 패널과 반응 제외)"""
    config = load_config("설정.txt")
    control_panel_title = config.get('control_panel_title')

    async for message in channel.history(limit=100):
        if message.embeds:
            embed = message.embeds[0]
            if embed.title == control_panel_title:
                continue
        await safe_delete_message(message)

async def cache_control_panel_message(channel):
    """컨트롤 패널 메시지 강제 캐싱"""
    config = load_config("설정.txt")
    control_panel_title = config.get('control_panel_title')

    async for message in channel.history(limit=100):
        if message.embeds and message.embeds[0].title == control_panel_title:
            bot._connection._messages.append(message)
            break
















#루프들
#-----------------------------------------------------------------------------------------------------------------
@tasks.loop(hours=24)
async def update_yt_dlp():
    try:
        print("yt-dlp 업데이트 시작...")
        subprocess.run(["pip", "install", "--upgrade", "yt-dlp"], check=True)
        print("yt-dlp 업데이트 완료!")
    except subprocess.CalledProcessError as e:
        print(f"업데이트 중 오류 발생: {e}")















#반응 관련
#---------------------------------------------------------------------------------------------------

@bot.event
async def on_reaction_add(reaction, user):
    global is_recent_expanded

    if user.bot:
        return

    config = load_config('설정.txt')
    channel_name = config['CHANNEL_NAME']
    embed_color = int(config['embed_color'], 16)

    if reaction.message.channel.name != channel_name:
        return

    try:
        await reaction.remove(user)
    except Exception as e:
        print(f"[ERROR] 반응 삭제 중 오류 발생: {e}")

    # ❓ 도움말
    if reaction.emoji == "❓":
        await send_help_dm(user)
        return

    # ⏹️ 정지 및 재시작
    if reaction.emoji == "⏹️":
        await stop_music(reaction.message.channel, music_state)
        return

    # 🗒️ 대기열 보기
    if reaction.emoji == "🗒️":
        if not music_state.queue:
            await reaction.message.channel.send(
                embed=create_embed(config['error_title'], config['error_message_queue_empty'], embed_color),
                delete_after=3)
        else:
            await show_queue(reaction.message.channel, music_state)
        return

    # ⏱️ 최근 재생 기록
    if reaction.emoji == "⏱️":
        if hasattr(music_state, "recent_tracks_message") and music_state.recent_tracks_message:
            try:
                await music_state.recent_tracks_message.delete()
            except discord.NotFound:
                pass
            music_state.recent_tracks_message = None
            return

        recent_tracks = load_recent_tracks()

        if not recent_tracks:
            msg = await reaction.message.channel.send(
                embed=create_embed("최근 재생 기록", "기록이 없습니다.", embed_color))
            await msg.delete(delay=3)
            return

        description = "\n".join([
            f"{i+1}. [{track['title']}]({track['url']})" +
            (f"\n   🎶 재생목록: [{track['playlist']['name']}]({track['playlist']['url']})"
             if 'playlist' in track else "")
            for i, track in enumerate(recent_tracks[:10])
        ])

        embed = create_embed("최근 재생 기록", description, embed_color)
        view = RecentTracksView(recent_tracks[:10], embed_color)
        music_state.recent_tracks_message = await reaction.message.channel.send(embed=embed, view=view)
        return

    # 🔁 반복 모드 토글
    if reaction.emoji == "🔁":
        music_state.repeat = not music_state.repeat
        state_text = "ON" if music_state.repeat else "OFF"
        await reaction.message.channel.send(
            embed=create_embed(config['repeat_mode_title'],
                               config['repeat_mode_message'].format(state_text=state_text, skip_text=""),
                               embed_color),
            delete_after=3)
        return

    # ⏯️ 일시정지/재생
    if reaction.emoji == "⏯️":
        if music_state.repeat:
            await reaction.message.channel.send(
                embed=create_embed(config['repeat_mode_title'], config['pause_error'], embed_color),
                delete_after=2)
            return
        await toggle_pause(reaction.message.channel, music_state)
        return

    # 🔀 셔플
    if reaction.emoji == "🔀":
        if music_state.repeat:
            await reaction.message.channel.send(
                embed=create_embed(config['repeat_mode_title'], config['shuffle_error'], embed_color),
                delete_after=2)
            return
        await shuffle_queue(reaction.message.channel, music_state)
        return

    # ⏭️ 건너뛰기
    if reaction.emoji == "⏭️":
        if music_state.repeat:
            await reaction.message.channel.send(
                embed=create_embed(config['repeat_mode_title'], config['skip_error'], embed_color),
                delete_after=2)
            return
        await skip_song(reaction.message.channel, music_state)
        return

    # ❌ 유효하지 않은 이모지
    valid_emojis = ["⏯️", "🔁", "⏭️", "🔀", "⏹️", "🗒️", "📜", "❓", "⏱️"]
    if reaction.emoji not in valid_emojis:
        await reaction.message.channel.send(
            embed=create_embed(config['error_title'], config['invalid_reaction_message'], embed_color),
            delete_after=2)

        
        

async def cleanup_voice_connection(music_state, channel):
    """🎤 음성 채널 연결 해제 및 정리"""
    print("[INFO] 음성 채널 연결 해제 및 정리 작업 시작...")

    if music_state.is_playing:
        music_state.queue.clear()
        music_state.is_playing = False
        print("[DEBUG] 음악 상태 초기화 완료")

    if music_state.last_played_embed:
        try:
            config = load_config("설정.txt")  # 대사 파일 로드
            embed_title = config.get('currently_playing_embed_title')  # 대사에서 제목 불러오기
            embed_description = config.get('currently_playing_embed_description')  # 대사에서 설명 불러오기
            embed_color = int(config.get('embed_color'), 16)  # 대사에서 색상 코드 불러오기

            embed = discord.Embed(title=embed_title, description=embed_description, color=embed_color)
            await music_state.last_played_embed.delete()
            music_state.last_played_embed = None
            print(f"[INFO] '{embed_title}' 임베드 삭제 완료.")
        except Exception as e:
            print(f"[ERROR] 임베드 삭제 실패: {e}")

    if music_state.current_song and music_state.current_song.get("is_local"):
        await cleanup_audio_file(music_state.current_song["url"])

    if music_state.voice_client and music_state.voice_client.is_connected():
        try:
            await music_state.voice_client.disconnect()
            print("[INFO] 음성 채널에서 연결 해제됨.")
        except Exception as e:
            print(f"[ERROR] 연결 해제 중 오류 발생: {e}")
        finally:
            music_state.voice_client = None

    await clean_channel_message(channel)

    music_state.voice_channel = None
    print("[INFO] 음성 채널 정리 완료.")

async def toggle_music_mode(channel, mode):
    """음악 모드(반복, 셔플, 일시정지 등) 토글"""
    config = load_config("설정.txt")  # 대사 파일 로드
    embed_color = int(config.get('embed_color'), 16)  # 대사에서 색상 코드 불러오기

    if not music_state.queue:
        message = config.get('queue_empty_message', "")  # 대사에서 메시지 불러오기
        await channel.send(embed=create_embed(message, f"{message} 상태로 전환", embed_color), delete_after=3)
        return

    if mode == "repeat":
        music_state.repeat = not music_state.repeat
        music_state.repeat_song = music_state.current_song if music_state.repeat else None
        state_text = config.get('repeat_mode_title', "")  # 대사에서 반복 모드 제목 불러오기
        state_description = f"{config.get('repeat_mode_description', '반복 모드가')} {'활성화' if music_state.repeat else '비활성화'}"  # 대사에서 설명 불러오기
        await channel.send(embed=create_embed(state_text, state_description, embed_color), delete_after=3)
    elif mode == "shuffle":
        if music_state.repeat:
            # 반복 상태에서 셔플은 불가능
            await channel.send(embed=create_embed(config.get('shuffle_error_title'),
                                                  config.get('shuffle_error_description'), embed_color),
                               delete_after=3)
            return
        random.shuffle(music_state.queue)
        await channel.send(embed=create_embed(config.get('shuffle_title'), config.get('shuffle_completed'), embed_color), delete_after=3)
    elif mode == "pause":
        if music_state.voice_client.is_playing():
            # 곡을 일시 정지
            music_state.voice_client.pause()
            music_state.is_playing = False
            music_state.is_paused = True
            await channel.send(embed=create_embed(config.get('pause_title'),
                                                  config.get('pause_message', ""), embed_color), delete_after=3)
        elif music_state.is_paused:
            # 일시 정지 상태에서 재개
            music_state.voice_client.resume()
            music_state.is_playing = True
            music_state.is_paused = False
            await channel.send(embed=create_embed(config.get('resume_title'),
                                                  config.get('resume_message', ""), embed_color), delete_after=3)

async def toggle_repeat(channel, music_state):
    """🔁: 반복 모드 토글"""
    config = load_config("설정.txt")  # 대사 파일 로드
    bot_name = config.get('bot_name')  # 대사에서 봇 이름 불러오기
    state = "ON" if music_state.repeat else "OFF"
    change_message = config.get('repeat_mode_change_message')  # 대사에서 반복 모드 변경 메시지 불러오기

    if music_state.repeat:
        # ✅ 현재 곡을 대기열 맨 앞으로 삽입 (반복 모드에서 끊김 방지)
        if music_state.current_song:
            music_state.queue.insert(0, music_state.current_song)
            music_state.repeat_song = music_state.current_song

        # ✅ 반복 모드 활성화 시 진행 시간 동기화
        music_state.current_start_time = time.time() - music_state.last_elapsed_time

        repeat_message = config.get('repeat_mode_enabled_message', "반복 모드가 활성화되었습니다.")  # 대사에서 반복 모드 활성화 메시지 불러오기
        await channel.send(embed=create_embed("🔁 반복 모드", repeat_message, int(config.get('embed_color'), 16)), delete_after=2)

    else:
        # ✅ 반복 모드 해제 시 진행 시간 기록 후 곡 제거
        music_state.last_elapsed_time = time.time() - music_state.current_start_time
        music_state.repeat_song = None  # 🔥 반복 곡 제거
        repeat_message = config.get('repeat_mode_disabled_message', "반복 모드가 비활성화되었습니다.")  # 대사에서 반복 모드 해제 메시지 불러오기
        await channel.send(embed=create_embed("🔁 반복 모드", repeat_message, int(config.get('embed_color'), 16)), delete_after=2)

    # ✅ 임베드 업데이트 (반복 상태 반영)
    if not update_embed_timer.is_running():
        update_embed_timer.start(channel, music_state)

async def add_repeat_song(channel, music_state):
    """🔄 반복 모드 활성화 시, 현재 곡을 자동으로 다시 대기열에 추가"""
    if not music_state.repeat or not music_state.current_song:
        return

    await asyncio.sleep(5)  # ✅ 현재 곡 재생 시작 후 5초 후에 다시 추가

    if music_state.repeat and music_state.current_song:
        music_state.queue.insert(0, music_state.current_song)
        print(f"🔁 반복 모드: '{music_state.current_song['title']}'을 다시 대기열에 추가했습니다.")

async def skip_song(channel, music_state):
    """⏭️ 현재 곡을 스킵하고 다음 곡을 재생"""
    config = load_config("설정.txt")  # 대사 파일 로드
    embed_color = int(config.get('embed_color'), 16)  # 대사에서 색상 코드 불러오기

    async with music_state.embed_lock:  # 🔒 락으로 동기화
        if not music_state.is_playing:
            error_message = config.get('no_song_playing_error')  # 대사에서 오류 메시지 불러오기
            error_title = config.get('error_title')  # 대사에서 오류 제목 불러오기
            await channel.send(embed=create_embed(error_title, error_message, embed_color), delete_after=3)
            return

        # 🔥 반복 모드가 활성화된 경우 스킵 방지
        if music_state.repeat:
            repeat_message = config.get('repeat_mode_skip_error')  # 대사에서 반복 모드 오류 메시지 불러오기
            repeat_mode_title = config.get('repeat_mode_title')  # 대사에서 반복 모드 제목 불러오기
            await channel.send(embed=create_embed(repeat_mode_title, repeat_message, embed_color), delete_after=2)
            return  # ⏭️ 스킵 방지

        # ✅ 마지막 곡인지 확인
        if not music_state.queue:  # 🎯 대기열이 비어 있음 → 마지막 곡
            last_song_message = config.get('last_song_error')  # 대사에서 마지막 곡 메시지 불러오기
            info_title = config.get('info_title')  # 대사에서 정보 제목 불러오기
            await channel.send(embed=create_embed(info_title, last_song_message, embed_color), delete_after=3)
            return  # 🎯 스킵 방지

        if music_state.last_played_embed:
            try:
                await music_state.last_played_embed.delete()
                music_state.last_played_embed = None
                music_state.is_embed_active = False
                print("[INFO] 기존 '재생' 임베드가 삭제되었어요.")
            except Exception as e:
                print(f"[ERROR] 임베드 삭제 중 오류 발생: {e}")

        # ✅ 현재 곡 정지
        if music_state.voice_client and music_state.voice_client.is_playing():
            music_state.voice_client.stop()
            await asyncio.sleep(1)  # ✅ 완전히 중지될 때까지 대기

        # ✅ 다음 곡 재생
        skip_message = config.get('skip_song_message')  # 대사에서 스킵 메시지 불러오기
        info_title = config.get('info_title')  # 대사에서 정보 제목 불러오기
        await channel.send(embed=create_embed(info_title, skip_message, embed_color), delete_after=0.5)

        # ✅ 곡을 완전히 중지한 후 `manage_audio_queue()` 호출
        if music_state.voice_client and not music_state.voice_client.is_playing():
            await manage_audio_queue(channel, music_state)  # 🚀 다음 곡 재생

async def manage_audio_queue(channel, music_state):
    """Audio queue management function to ensure next song plays"""
    if not music_state.queue:  # If there's no song in the queue
        music_state.is_playing = False
        return

    if music_state.is_playing_next_song:  # Prevent overlapping tasks
        print("[INFO] Next song is already being played. Skipping this task.")
        return

    if not music_state.voice_client or not music_state.voice_client.is_connected():
        if music_state.voice_channel:
            print("[INFO] 음성 채널 재연결 시도...")
            await music_state.connect_to_channel(music_state.voice_channel)  # 연결 시도
        else:
            print("[ERROR] 음성 채널이 설정되지 않음. 재생 불가.")
            return

async def show_queue(channel, music_state):
    config = load_config("설정.txt")
    embed_color = int(config.get('embed_color'), 16)
    queue_title = config.get('queue_title', "대기열")
    queue_list_text = config.get('queue_list', "대기열이 비어 있습니다.")
    channel_name = config.get("CHANNEL_NAME", "")

    # ✅ 기존 같은 타이틀의 대기열 임베드 삭제 (토글 접기 기능)
    try:
        async for msg in channel.history(limit=50):
            if msg.embeds and msg.embeds[0].title == queue_title:
                await msg.delete()
                print(f"[INFO] '{queue_title}' 임베드를 삭제했습니다.")
                return  # 토글 종료 (접기)
    except Exception as e:
        print(f"[ERROR] 임베드 삭제 실패: {e}")

    # ✅ 대기열 내용 구성 (비어 있으면 대사 표시)
    if music_state.queue:
        description = "\n".join(
            [f"{index + 1}. {song['title']}" for index, song in enumerate(music_state.queue)]
        )
    else:
        description = queue_list_text

    # ✅ 새로운 임베드 생성 (펼치기)
    embed = discord.Embed(
        title=queue_title,
        description=description,
        color=embed_color
    )

    try:
        message = await channel.send(embed=embed)
        music_state.queue_message = message
        print(f"[INFO] '{queue_title}' 임베드를 생성했습니다.")
    except Exception as e:
        print(f"[ERROR] 대기열 메시지 전송 중 오류 발생: {e}")

async def shuffle_queue(channel, music_state):
    """대기열 셔플"""
    config = load_config("설정.txt")  # 대사 파일 로드
    embed_color = int(config.get('embed_color'), 16)  # 대사에서 색상 코드 불러오기

    if len(music_state.queue) < 2:
        shuffle_fail_title = config.get('shuffle_fail_title')  # 셔플 실패 제목
        shuffle_fail_message = config.get('shuffle_fail_message')  # 셔플 실패 메시지
        await channel.send(embed=create_embed(shuffle_fail_title, shuffle_fail_message, embed_color), delete_after=3)
        return

    random.shuffle(music_state.queue)
    shuffle_complete_title = config.get('shuffle_complete_title')  # 셔플 완료 제목
    shuffle_complete_message = config.get('shuffle_complete_message')  # 셔플 완료 메시지
    await channel.send(embed=create_embed(shuffle_complete_title, shuffle_complete_message, embed_color), delete_after=3)

async def toggle_pause(channel, music_state):
    """음악을 일시정지 또는 재개"""
    config = load_config("설정.txt")  # 대사 파일 로드
    embed_color = int(config.get('embed_color'), 16)  # 대사에서 색상 코드 불러오기

    # 음성 채널 연결 확인
    if not music_state.voice_client or not music_state.voice_client.is_connected():
        error_title = config.get('error_title')  # 오류 제목
        error_message = config.get('no_voice_channel')  # 오류 메시지
        await channel.send(embed=create_embed(error_title, error_message, embed_color), delete_after=3)
        return

    if music_state.is_paused:  # 일시정지 해제
        music_state.voice_client.resume()
        music_state.is_paused = False  # 일시정지 상태 해제

        # 일시정지된 시간을 바탕으로 current_start_time을 재설정
        music_state.current_start_time = time.time() - music_state.last_elapsed_time

        # 타이머 재시작
        if not update_embed_timer.is_running():
            update_embed_timer.start(channel, music_state)

        resume_title = config.get('resume_title')  # 재개 제목
        resume_message = config.get('resume_message')  # 재개 메시지
        await channel.send(embed=create_embed(resume_title, resume_message, embed_color), delete_after=2)
    else:  # 일시정지
        music_state.voice_client.pause()
        music_state.is_paused = True  # 일시정지 상태로 설정

        # 현재 시간을 기록하여 last_elapsed_time을 저장
        music_state.last_elapsed_time = time.time() - music_state.current_start_time

        # 타이머 멈추기
        update_embed_timer.stop()

        pause_title = config.get('pause_title')  # 일시 정지 제목
        pause_message = config.get('pause_message')  # 일시 정지 메시지
        await channel.send(embed=create_embed(pause_title, pause_message, embed_color), delete_after=2)

    # is_playing 플래그를 유지하여 재생 중으로 취급
    music_state.is_playing = True  # 일시정지 상태에서도 True로 유지


async def stop_music(channel, music_state):
    global is_restart_in_progress
    print("[INFO] 봇 로그아웃 시도 중...")

    if is_restart_in_progress:
        print("[INFO] 이미 재시작 중입니다. 재시작을 건너뜁니다.")
        return

    try:
        is_restart_in_progress = True

        # 현재 실행 중인 .exe 파일 경로를 직접 가져옴
        exe_path = os.path.join(os.getcwd(), "start.exe")

        if not os.path.exists(exe_path):
            print(f"[ERROR] 재시작 대상 파일을 찾을 수 없습니다: {exe_path}")
            return

        print(f"[INFO] {exe_path} 재시작 시도 중... (5초 후)")
        await asyncio.sleep(5.0)  # 안전한 재실행을 위한 딜레이

        subprocess.Popen([exe_path, "--restarted"], creationflags=subprocess.DETACHED_PROCESS)

        print("[INFO] 재시작 명령 완료. 봇 종료 중...")
        await bot.close()

    except Exception as e:
        print(f"[ERROR] 재시작 중 오류 발생: {e}")

    finally:
        is_restart_in_progress = False

def main():
    try:
        # 설정 파일에서 봇 토큰 읽기
        config = load_config("설정.txt")  # 통합된 설정 파일을 읽습니다.

        BOT_TOKEN = config.get("BOT_TOKEN")  # 봇 토큰을 가져옵니다.

        if not BOT_TOKEN:
            raise ValueError("설정 파일에 봇 토큰이 없습니다.")

        # 봇 실행
        bot.run(BOT_TOKEN, reconnect=True)

    except FileNotFoundError:
        print("설정 파일을 찾을 수 없습니다. 파일 경로를 확인해주세요.")
    except ValueError as e:
        print(f"오류: {e}")
    except Exception as e:
        print(f"예기치 못한 오류가 발생했습니다: {e}")


if __name__ == "__main__":
    main()