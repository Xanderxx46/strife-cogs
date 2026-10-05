import asyncio
from typing import Any, Optional

import aiohttp
import discord
from redbot.core import app_commands, commands

PK_API = "https://api.pluralkit.me/v2"
_TITLE_LIMIT = 256
_DESC_LIMIT = 4096
_FIELD_LIMIT = 1024


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _member_label(member: dict) -> str:
    return member.get("display_name") or member.get("name") or "Unknown"


def _error_code(data: Any) -> Optional[str]:
    if isinstance(data, dict) and "error" in data and set(data) <= {"error", "retry_after"}:
        return str(data["error"])
    return None


def _embed_color(color_hex: Optional[str]) -> discord.Color:
    if not color_hex:
        return discord.Color.default()
    try:
        return discord.Color(int(str(color_hex).removeprefix("#"), 16))
    except ValueError:
        return discord.Color.default()


class PKLens(commands.Cog):
    """A privacy-focused PluralKit inspector for Discord Context Menus and Slash Commands."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.headers = {"User-Agent": "PKLens/1.0 (https://github.com/SinOfStrife/strife-cogs)"}

        self.check_fronters_menu = app_commands.ContextMenu(
            name="fronters",
            callback=self.check_fronter_callback,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
        )
        self.view_profile_menu = app_commands.ContextMenu(
            name="profile",
            callback=self.view_profile_callback,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
        )

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=20),
        )
        try:
            self.bot.tree.add_command(self.check_fronters_menu)
            self.bot.tree.add_command(self.view_profile_menu)
        except Exception:
            self._drop_menus()
            await self.session.close()
            self.session = None
            raise

    async def cog_unload(self) -> None:
        self._drop_menus()
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

    def _drop_menus(self) -> None:
        for menu in (self.check_fronters_menu, self.view_profile_menu):
            if self.bot.tree.get_command(menu.name, type=menu.type) is not None:
                self.bot.tree.remove_command(menu.name, type=menu.type)

    async def fetch_pk_data(self, endpoint: str) -> dict:
        """Fetch PluralKit data. Network failures become an error payload instead of a traceback."""
        if self.session is None or self.session.closed:
            return {"error": "api_error"}

        try:
            async with self.session.get(f"{PK_API}{endpoint}") as resp:
                if resp.status == 404:
                    return {"error": "not_found"}
                if resp.status == 403:
                    return {"error": "private"}
                if resp.status == 429:
                    return {"error": "rate_limited", "retry_after": resp.headers.get("Retry-After")}
                if resp.status != 200:
                    return {"error": "api_error"}
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return {"error": "api_error"}

        if not isinstance(payload, dict):
            return {"error": "api_error"}
        return payload

    async def _send_pk_error(self, interaction: discord.Interaction, user: discord.User, data: dict, kind: str):
        """Send a consistent user-only error message for the selected PK check."""
        error = _error_code(data) or "api_error"
        if error == "not_found":
            msg = f"**{user.name}** is either not registered with PluralKit or does not have a public profile."
        elif error == "private":
            if kind == "system":
                msg = f"**{user.name}** has set their system profile to **private**."
            else:
                msg = f"**{user.name}** has set their fronter information to **private**."
        elif error == "rate_limited":
            retry = _retry_seconds(data.get("retry_after"))
            if retry is None:
                msg = "PluralKit is rate-limiting requests right now. Please try again in a moment."
            else:
                msg = f"PluralKit is rate-limiting requests right now. Please try again in {retry} seconds."
        else:
            msg = "An error occurred while communicating with the PluralKit API."

        embed = discord.Embed(title="⚠️ Error", description=msg, color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _reply_fronters(self, interaction: discord.Interaction, user: discord.User) -> None:
        data = await self.fetch_pk_data(f"/systems/{user.id}/fronters")
        if _error_code(data):
            await self._send_pk_error(interaction, user, data, "fronters")
            return

        fronters = data.get("members") or []
        if not isinstance(fronters, list):
            await self._send_pk_error(interaction, user, {"error": "api_error"}, "fronters")
            return

        if not fronters:
            embed = discord.Embed(
                title=_clip(f"🟢 Current Fronters: {user.name}", _TITLE_LIMIT),
                description="No system members are currently fronting.",
                color=discord.Color.dark_grey(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        names = [_member_label(member) for member in fronters if isinstance(member, dict)]
        description = _clip(", ".join(names) if names else "Unknown", _DESC_LIMIT)
        embed = discord.Embed(
            title=_clip(f"🟢 Current Fronters: {user.name}", _TITLE_LIMIT),
            description=description,
            color=discord.Color.green(),
        )
        for member in fronters:
            if isinstance(member, dict) and member.get("avatar_url"):
                embed.set_thumbnail(url=member["avatar_url"])
                break
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _reply_profile(self, interaction: discord.Interaction, user: discord.User) -> None:
        data = await self.fetch_pk_data(f"/systems/{user.id}")
        if _error_code(data):
            await self._send_pk_error(interaction, user, data, "system")
            return

        system_name = data.get("name") or user.name
        tag = data.get("tag")
        system_title = f"{system_name} [{tag}]" if tag else str(system_name)
        description = data.get("description") or "No description provided."
        pronouns = data.get("pronouns") or "Not specified."

        embed = discord.Embed(
            title=_clip(system_title, _TITLE_LIMIT),
            description=_clip(str(description), _DESC_LIMIT),
            color=_embed_color(data.get("color")),
        )
        embed.add_field(name="Pronouns", value=_clip(str(pronouns), _FIELD_LIMIT), inline=True)
        if data.get("avatar_url"):
            embed.set_thumbnail(url=data["avatar_url"])
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="pklens", description="View info about the PKLens app and how to use it.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(public="Set to True to share the info message with the channel (default: False)")
    async def pklens_help(self, interaction: discord.Interaction, public: bool = False):
        embed = discord.Embed(
            title="🔍 PKLens",
            description=(
                "A lightweight, privacy-focused tool designed to make viewing public PluralKit "
                "system profiles and current fronters accessible across Discord."
            ),
            color=discord.Color.from_str("#6b2598"),
        )
        embed.add_field(
            name="How to use",
            value="Use slash commands or right-click any user via their profile (`Apps` ➔ `fronters` or `profile`).",
            inline=False,
        )
        embed.add_field(
            name="Privacy",
            value=(
                "Lookups are ephemeral (only you see them). `/pklens` can be made public to introduce the app to others. "
                "A lookup still sends that Discord user ID to the PluralKit API."
            ),
            inline=False,
        )
        embed.set_footer(text="Inspired by the Gayos of chaos")
        await interaction.response.send_message(embed=embed, ephemeral=not public)

    @app_commands.command(
        name="pkfronters",
        description="Check who is currently fronting in a user's PluralKit system.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def pkfronters_slash(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        await self._reply_fronters(interaction, user)

    @app_commands.command(name="pkprofile", description="View a user's PluralKit system profile.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def pkprofile_slash(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        await self._reply_profile(interaction, user)

    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def check_fronter_callback(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        await self._reply_fronters(interaction, user)

    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def view_profile_callback(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        await self._reply_profile(interaction, user)


def _retry_seconds(value: Any) -> Optional[str]:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    if seconds < 0 or seconds > 3600:
        return None
    return str(seconds)
