import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import datetime
from typing import Optional
from collections import defaultdict
from typing import List
import asyncio


from database import DatabaseManager
from .utils import create_controller_embed

VATSIM_DATA_URL = "https://data.vatsim.net/v3/vatsim-data.json"

# --- Permission Check from db ---
async def check_manager_permissions(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    
    db_manager = DatabaseManager()
    manager_role_id = await db_manager.get_management_role(interaction.guild_id)
    
    if manager_role_id and any(role.id == manager_role_id for role in interaction.user.roles):
        return True
        
    await interaction.response.send_message("You need to be an Administrator or have the manager role to use this command.", ephemeral=True)
    return False

# --- UI View for Removing Notifications ---
class RemoveNotificationView(discord.ui.View):
    def __init__(self, bot: commands.Bot, notifications: list):
        super().__init__(timeout=180)
        self.bot = bot
        self.db_manager = DatabaseManager()
        
        options = []
        for notification in notifications:
            channel = bot.get_channel(notification[2])
            channel_name = f"#{channel.name}" if channel else f"ID: {notification[2]}"
            options.append(discord.SelectOption(
                label=f"{notification[1]} in {channel_name}",
                value=str(notification[0]),
                emoji="🗑️"
            ))

        select_menu = discord.ui.Select(placeholder="Select a notification to remove...", options=options, custom_id="remove_select")
        select_menu.callback = self.select_callback
        self.add_item(select_menu)

    async def select_callback(self, interaction: discord.Interaction):
        selection_id = int(interaction.data['values'][0])
        await self.db_manager.remove_notification(selection_id)
        await interaction.response.send_message(f"✅ Notification has been removed.", ephemeral=True)

class AtcCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_manager = DatabaseManager()
        self.previously_notified = set()
        self.vatsim_checker.start()
        self.update_controller_trackers.start()

    def cog_unload(self):
        self.vatsim_checker.cancel()
        self.update_controller_trackers.cancel()

    atcnotify = app_commands.Group(name="atcnotify", description="Commands for ATC notifications.")

    @tasks.loop(minutes=4)
    async def vatsim_checker(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(VATSIM_DATA_URL) as response:
                    if response.status != 200:
                        print(f"Error fetching VATSIM data: Status {response.status}")
                        return
                    data = await response.json()
        except aiohttp.ClientError as e:
            print(f"AIOHTTP Error fetching VATSIM data: {e}")
            return

        try:
            total_guilds = len(self.bot.guilds)
            total_members = sum(guild.member_count for guild in self.bot.guilds)
            airport_count = await self.db_manager.get_watched_airport_count()
            
            activity_string = f"{total_guilds} airports, {total_members} pilots, {airport_count} ATC Notifications Set | /help"
            activity = discord.Activity(name=activity_string, type=discord.ActivityType.watching)
            await self.bot.change_presence(activity=activity)
        except Exception as e:
            print(f"Error updating presence: {e}")

        current_controllers = {controller['callsign'] for controller in data.get('controllers', [])}
        all_rules = await self.db_manager.get_all_notifications()
        
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        print(f"[{timestamp}] Processing ATC notifications: {len(all_rules)} rule(s), {len(current_controllers)} controller(s) online")
        print(f"[{timestamp}] Currently tracking {len(self.previously_notified)} previously notified controller(s)")
        
        pending_notifications = defaultdict(list)

        banned_frequencies = ["199.998", "199.997", "199.999"]
        
        for rule in all_rules:
            rule_id, guild_id, airport_icao, channel_id, role_id, delete_pref = rule
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
            print(f"[{timestamp}] === Checking rule {rule_id} for {airport_icao} ===")
            
            for controller in data.get('controllers', []):
                callsign = controller['callsign']
                
                if "OBS" in callsign.upper() or controller['frequency'] in banned_frequencies:
                    continue

                db_identifier = airport_icao
                callsign_base = callsign.split('_')[0]

                # Create a set of valid identifiers to check against
                # Global ICAO matching: check full code, last 3 chars, and last 2 chars
                identifiers_to_check = {db_identifier}
                
                # Add last 3 characters (e.g., YSSY -> SSY, KORD -> ORD, YMML -> MML)
                if len(db_identifier) >= 3:
                    identifiers_to_check.add(db_identifier[-3:])
                
                # Add last 2 characters (e.g., YSSY -> SY, YMML -> ML, EGLL -> LL)
                if len(db_identifier) >= 2:
                    identifiers_to_check.add(db_identifier[-2:])
                
                # For 4-letter codes starting with K (US convention), also add 3-letter version
                # e.g., KORD -> ORD (already covered by last 3 chars above)
                if len(db_identifier) == 4 and db_identifier.startswith('K'):
                    identifiers_to_check.add(db_identifier[1:])

                match_found = callsign_base in identifiers_to_check
                
                # Detailed logging for debugging matches
                timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
                if callsign_base == db_identifier[:2] or callsign_base == db_identifier[-2:]:
                    # Potential match that's failing due to logic
                    print(f"[{timestamp}] 🔍 Comparing {callsign}: base='{callsign_base}' vs identifiers={identifiers_to_check} -> {'MATCH' if match_found else 'NO MATCH (but looks related!)'}")
                
                if match_found:
                    # Use logon_time to track unique sessions
                    logon_time = controller.get('logon_time', '')
                    session_key = (rule_id, callsign, logon_time)
                    
                    if session_key in self.previously_notified:
                        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
                        print(f"[{timestamp}] Skipping {callsign} for rule {rule_id} ({airport_icao}): Already notified this session (logged on at {logon_time})")
                    else:
                        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
                        print(f"[{timestamp}] ✓ NEW MATCH: {callsign} matches rule {rule_id} ({airport_icao}) - New session logged on at {logon_time}")
                        key = (rule_id, guild_id, channel_id, role_id, airport_icao, delete_pref)
                        pending_notifications[key].append(controller)
        
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        if pending_notifications:
            print(f"[{timestamp}] Preparing to send {len(pending_notifications)} notification(s)")
        else:
            print(f"[{timestamp}] No new notifications to send")
        
        for (rule_id, guild_id, channel_id, role_id, airport_icao, delete_pref), controllers_list in pending_notifications.items():
            guild = self.bot.get_guild(guild_id)
            channel = self.bot.get_channel(channel_id)
            role = guild.get_role(role_id) if guild and role_id else None
            
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
            if not guild:
                print(f"[{timestamp}] ⚠️ Cannot send notification for rule {rule_id}: Guild {guild_id} not found")
                continue
            if not channel:
                print(f"[{timestamp}] ⚠️ Cannot send notification for rule {rule_id}: Channel {channel_id} not found")
                continue

            print(f"[{timestamp}] 📤 Sending notification: {[c['callsign'] for c in controllers_list]} for rule {rule_id} to #{channel.name} in {guild.name}")

            title = f"📡 ATC Online at {airport_icao}"
            description = ""
            for controller in sorted(controllers_list, key=lambda c: c['callsign']):
                description += f"**`{controller['callsign']}`** ({controller['frequency']}) - {controller['name']}\n"
            
            embed = discord.Embed(title=title, description=description, color=discord.Color.blue(), timestamp=datetime.datetime.now(datetime.timezone.utc))
            
            prefixes_to_check = {airport_icao}
            # If the identifier is a 3-letter code (common for US airports), also check for its 'K'-prefixed version.
            if len(airport_icao) == 3:
                prefixes_to_check.add(f"K{airport_icao}")
            # If the identifier is a 4-letter 'K' code, also check for its 3-letter version.
            elif len(airport_icao) == 4 and airport_icao.startswith('K'):
                prefixes_to_check.add(airport_icao[1:])

            matching_atis_list = []
            for atis in data.get('atis', []):
                if any(atis['callsign'].startswith(prefix) for prefix in prefixes_to_check):
                    matching_atis_list.append(atis)


            # If an ATIS was found for the airport
            if matching_atis_list:
                for atis in sorted(matching_atis_list, key=lambda a: a['callsign']):
                    atis_type_name = atis['callsign'].replace(f"{airport_icao}_", "").replace("_ATIS", "")
                    if atis_type_name in ['D', 'A']:
                        atis_type_name = {'D': 'Departure', 'A': 'Arrival'}.get(atis_type_name, atis_type_name)
                    atis_field_name = f"ATIS ({atis_type_name})" if atis_type_name else "ATIS"
                    
                    atis_lines = atis.get('text_atis')
                    if atis_lines:
                        atis_text = "\n".join(atis_lines)
                        if len(atis_text) > 1000: # Truncate long ATIS messages
                            atis_text = atis_text[:1000] + "..."
                        embed.add_field(name=atis_field_name, value=f"```\n{atis_text}\n```", inline=False)
                    else:
                        # Handles the rare case where an ATIS object exists but has no text
                        embed.add_field(name=atis_field_name, value="ATIS information not available.", inline=False)

            # If no ATIS was found at all for the airport
            else:
                # Check if all controllers in the notification are non-terminal
                is_non_terminal_only = all(
                    '_APP' in c['callsign'] or 
                    '_DEP' in c['callsign'] or 
                    '_CTR' in c['callsign'] 
                    for c in controllers_list
                )

                if is_non_terminal_only:
                    atis_message = "Approach/Center positions do not have a dedicated ATIS."
                else:
                    atis_message = "No active ATIS found."
                
                embed.add_field(name="ATIS", value=atis_message, inline=False)
            
            embed.set_footer(text="Vatsim ATC Notifier")
            
            try:
                content_to_send = role.mention if role else None
                sent_message = await channel.send(content=content_to_send, embed=embed)
                
                timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
                print(f"[{timestamp}] ✅ Notification sent successfully (Message ID: {sent_message.id})")

                for controller in controllers_list:
                    logon_time = controller.get('logon_time', '')
                    session_key = (rule_id, controller['callsign'], logon_time)
                    self.previously_notified.add(session_key)
                    
                    if delete_pref:
                        await self.db_manager.add_active_notification(rule_id, sent_message.id, channel.id, controller['callsign'])
                        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
                        print(f"[{timestamp}] 💾 Added {controller['callsign']} (session: {logon_time}) to active_notifications (delete_on_offline enabled)")
                    else:
                        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
                        print(f"[{timestamp}] 📝 Added {controller['callsign']} (session: {logon_time}) to in-memory cache")
            except discord.Forbidden:
                timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
                print(f"[{timestamp}] ❌ PERMISSION ERROR: Cannot send to #{channel.name} in {guild.name} (G:{guild.id} C:{channel.id})")
                try:
                    owner = guild.owner
                    if not owner: # Fallback if owner is not cached
                        owner = await self.bot.fetch_user(guild.owner_id)

                    error_embed = discord.Embed(
                        title="⚠️ Permission Error",
                        description=f"Hello! I was unable to send an ATC notification in your server **{guild.name}**.",
                        color=discord.Color.red(),
                        timestamp=datetime.datetime.now(datetime.timezone.utc)
                    )
                    error_embed.add_field(name="Problem Channel", value=f"<#{channel.id}> (`{channel.id}`)", inline=False)
                    error_embed.add_field(name="Required Permissions", value="• Send Messages\n• Embed Links", inline=False)
                    error_embed.set_footer(text="Please update my role permissions in that channel.")
                    
                    await owner.send(embed=error_embed)
                    print(f"--> Sent permission error DM to owner {owner} for guild {guild.id}")
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    # If it fails to send not much I can do sucks to suck :/
                    print(f"--> Could not send permission error DM to owner of guild {guild.id}.")
            except Exception as e:
                print(f"An error occurred sending notification: {e}")

        # Clean up offline controllers - only match by callsign (ignore logon_time for cleanup)
        current_callsigns = {controller['callsign'] for controller in data.get('controllers', [])}
        offline_callsigns = {notified[1] for notified in self.previously_notified} - current_callsigns
        
        for callsign in offline_callsigns:
            active_notif_record = await self.db_manager.get_active_notification_by_callsign(callsign)
            
            if active_notif_record:
                _, message_id, channel_id = active_notif_record
                try:
                    channel = self.bot.get_channel(channel_id)
                    if channel:
                        message = await channel.fetch_message(message_id)
                        await message.delete()
                        print(f"Deleted notification message {message_id} for offline controller {callsign}.")
                except (discord.NotFound, discord.Forbidden):
                    pass
                except Exception as e:
                    print(f"Could not delete notification message {message_id}: {e}")
                
                await self.db_manager.remove_active_notification_by_callsign(callsign)

        # Remove offline sessions from cache (check callsign in position 1 of tuple)
        self.previously_notified = {notified for notified in self.previously_notified if notified[1] in current_callsigns}

    @vatsim_checker.before_loop
    async def before_vatsim_checker(self):
        await self.bot.wait_until_ready()
        print("Rehydrating notification cache from database...")
        try:
            # This loads already-notified controllers (for deletion) into memory on startup
            active_pairs = await self.db_manager.get_all_active_rule_callsign_pairs()
            self.previously_notified = set(active_pairs)
            print(f"--> Rehydrated {len(self.previously_notified)} active notifications from the database.")
        except Exception as e:
            print(f"Error during notification cache rehydration: {e}")

    # --- Commands ---
    @app_commands.command(name="help", description="Shows information about the ATC Notifier Bot.")
    async def help_command(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="ATC Notifier Help",
            description="This bot provides notifications when VATSIM ATC for a specific airport comes online.",
            color=discord.Color.dark_green()
        )
        embed.add_field(name="/atcnotify add `identifier` `channel` `[role]` `[delete_message]`", value="Sets up a new notification. The `role` and `delete_message` parameters are optional. Requires manager permissions.", inline=False)
        embed.add_field(name="/atcnotify remove", value="Shows a list of your server's notifications to remove one. Requires manager permissions.", inline=False)
        embed.add_field(name="/atcnotify list", value="Lists all currently configured notifications for this server.", inline=False)
        embed.add_field(name="/atcnotify config-role `role`", value="Sets a role that can manage notifications (Admin only).", inline=False)
        embed.add_field(name="/track-pilot `cid` `channel` `[role]` `[delete_on_offline]`", value="Tracks a pilot by CID and posts updates in the specified channel. The `role` and `delete_on_offline` parameters are optional.", inline=False)
        embed.add_field(name="/untrack-pilot `cid`", value="Stops tracking a pilot by CID.", inline=False)
        embed.add_field(name="/track-controller `cid` `channel` `[role]` `[delete_on_offline]`", value="Tracks a controller by CID and posts updates in the specified channel. The `role` and `delete_on_offline` parameters are optional.", inline=False)
        embed.add_field(name="/untrack-controller `cid`", value="Stops tracking a controller by CID.", inline=False)
        embed.add_field(name="/lookup atc `callsign`", value="Looks up a specific online controller.", inline=False)
        embed.add_field(name="/lookup atis `airport`", value="Gets the current ATIS for an airport.", inline=False)
        embed.add_field(name="activity", value="Shows all online activity for a specific airport.", inline=False)
        embed.set_footer(text="Made by Im2Slothy#0 - Support Discord https://discord.gg/RQBhmWEzTx")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @atcnotify.command(name="add", description="Add an ATC Airport or Center notification.")
    @app_commands.describe(
        identifier="The airport ICAO or Center prefix (e.g., KORD, CLE).", 
        channel="The channel to send notifications in.", 
        role="The role to ping for the notification (Optional).",
        delete_message="Set to True to delete the notification message when the controller goes offline."
    )
    @app_commands.check(check_manager_permissions)
    async def add(self, interaction: discord.Interaction, identifier: str, channel: discord.TextChannel, role: Optional[discord.Role] = None, delete_message: bool = False):
        # Defer the interaction immediately.
        await interaction.response.defer()

        if len(identifier) < 3 or len(identifier) > 4:
            await interaction.followup.send("The identifier must be 3 or 4 letters long.", ephemeral=True)
            return
        
        role_id = role.id if role else None
        identifier_upper = identifier.upper()

        # Check if this exact notification already exists to prevent duplicates.
        exists = await self.db_manager.notification_exists(interaction.guild_id, identifier_upper, channel.id, role_id)
        if exists:
            await interaction.followup.send(f"This exact notification for **{identifier_upper}** already exists in {channel.mention}.")
            return

        # If it doesn't exist, add it to the database.
        await self.db_manager.add_notification(interaction.guild_id, identifier_upper, channel.id, role_id, delete_message)

        ping_text = f"and ping {role.mention} " if role else ""
        response_text = f"✅ Success! I will now notify in {channel.mention} {ping_text}when ATC for **{identifier_upper}** comes online."

        if delete_message:
            response_text += "\n*The notification message will be deleted when the controller goes offline.*"

        await interaction.followup.send(response_text)

    @atcnotify.command(name="list", description="List all active ATC notifications for this server.")
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        notifications = await self.db_manager.get_notifications_by_guild(interaction.guild_id)
        if not notifications:
            await interaction.followup.send("There are no active notifications for this server.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Active Notifications for {interaction.guild.name}", color=discord.Color.og_blurple())
        description = ""
        for notif in notifications:
            channel = self.bot.get_channel(notif[2])
            role_id = notif[3]
            role = interaction.guild.get_role(role_id) if role_id else None

            ping_text = f"pings {role.mention}" if role else "pings no role"
            channel_text = channel.mention if channel else "Unknown Channel"

            description += f"• **{notif[1]}** -> {channel_text} ({ping_text})\n"
        embed.description = description
        await interaction.followup.send(embed=embed, ephemeral=True)

    @atcnotify.command(name="remove", description="Remove an active ATC notification.")
    @app_commands.check(check_manager_permissions)
    async def remove(self, interaction: discord.Interaction):
        notifications = await self.db_manager.get_notifications_by_guild(interaction.guild_id)
        if not notifications:
            await interaction.response.send_message("There are no active notifications to remove.", ephemeral=True)
            return
        view = RemoveNotificationView(self.bot, notifications)
        await interaction.response.send_message("Please select a notification to remove from the dropdown below:", view=view, ephemeral=True)
        
    @atcnotify.command(name="config-role", description="Set a role that can manage ATC notifications (Admin only).")
    @app_commands.describe(role="The role that will be allowed to manage notifications.")
    @app_commands.checks.has_permissions(administrator=True)
    async def config_role(self, interaction: discord.Interaction, role: discord.Role):
        await self.db_manager.set_management_role(interaction.guild_id, role.id)
        await interaction.response.send_message(f"✅ The {role.mention} role can now manage ATC notifications.", ephemeral=True)
    
    @app_commands.command(name="force-update", description="Force an immediate update of all trackers and notifications (Admin only).")
    @app_commands.checks.has_permissions(administrator=True)
    async def force_update(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        print(f"[{timestamp}] 🔄 MANUAL UPDATE TRIGGERED by {interaction.user.name}")
        
        # Trigger the loops manually
        await interaction.followup.send("⏳ Running ATC notification check...", ephemeral=True)
        await self.vatsim_checker()
        
        await interaction.followup.send("⏳ Running controller tracker update...", ephemeral=True)
        await self.update_controller_trackers()
        
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        print(f"[{timestamp}] ✅ Manual update completed")
        
        await interaction.followup.send("✅ All updates completed! Check the console for detailed logs.", ephemeral=True)
    
    @app_commands.command(name="restart-bot", description="Restart the bot (Admin only).")
    @app_commands.checks.has_permissions(administrator=True)
    async def restart_bot(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔄 Restarting in 3 seconds...", ephemeral=True)
        
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        print(f"[{timestamp}] 🔄 RESTART TRIGGERED by {interaction.user.name}")
        
        # Give Discord time to send the response message
        await asyncio.sleep(3)
        
        print(f"[{timestamp}] Shutting down gracefully...")
        
        # Close the bot cleanly
        await self.bot.close()
        
        # Exit with code 1 so startbot.py will restart it
        import sys
        sys.exit(1)
        
    @app_commands.command(name="track-controller", description="Continuously track a controller's status in a specific channel.")
    @app_commands.describe(
        cid="The VATSIM CID of the controller to track.", 
        channel="The channel to post the tracking embed in.",
        role="The role to ping when the controller comes online (Optional).",
        delete_on_offline="Set to True to delete the message when the controller logs off.",
        nickname="A friendly name for this controller (Optional)."
    )
    @app_commands.check(check_manager_permissions)
    async def track_controller(self, interaction: discord.Interaction, cid: str, channel: discord.TextChannel, delete_on_offline: bool = False, role: Optional[discord.Role] = None, nickname: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        existing = await self.db_manager.get_controller_tracker_by_cid(interaction.guild_id, cid)
        if existing:
            await interaction.followup.send(f"A controller tracker for CID `{cid}` already exists.", ephemeral=True)
            return

        title = f"Initializing Controller Tracker for CID: {cid}"
        if nickname:
            title = f"Initializing Controller Tracker: {nickname} (CID: {cid})"
            
        embed = discord.Embed(title=title, description="Fetching initial data...", color=discord.Color.light_grey())
        try:
            message = await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to send messages in that channel.", ephemeral=True)
            return
        
        role_id = role.id if role else None
        await self.db_manager.add_controller_tracker(interaction.guild_id, channel.id, message.id, cid, delete_on_offline, role_id, nickname)
        
        response_text = f"✅ Controller tracker for CID `{cid}` created in {channel.mention}."
        if nickname:
            response_text = f"✅ Controller tracker for **{nickname}** (CID `{cid}`) created in {channel.mention}."
        if role:
            response_text += f"\n*I will ping {role.mention} when the controller comes online.*"
        if delete_on_offline:
            response_text += "\n*This message will be deleted when the controller goes offline.*"
            
        await interaction.followup.send(response_text, ephemeral=True)
        # Run an immediate update
        await self.update_specific_controller_tracker(message.id, channel.id, cid)

    @app_commands.command(name="untrack-controller", description="Stops tracking a controller.")
    @app_commands.describe(cid="The VATSIM CID of the controller to untrack.")
    async def untrack_controller(self, interaction: discord.Interaction, cid: str):
        if not await check_manager_permissions(interaction):
            return  # The check function sends the "no permission" message

        await interaction.response.defer(ephemeral=True)

        # Strip 'CID: ' prefix if present (handles autocomplete format or user input)
        cid = cid.replace('CID:', '').replace('CID ', '').strip()

        tracker = await self.db_manager.get_controller_tracker_by_cid(interaction.guild_id, cid)
        if not tracker:
            await interaction.followup.send(f"No controller tracker found for CID `{cid}`.", ephemeral=True)
            return

        tracker_id, _, channel_id, message_id, _, _, _, _ = tracker # Unpack tracker data from DB 
        try:
            channel = self.bot.get_channel(channel_id)
            if channel and message_id:
                message = await channel.fetch_message(message_id)
                await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        await self.db_manager.remove_controller_tracker(tracker_id)
        await interaction.followup.send(f"✅ The controller tracker for CID `{cid}` has been removed.", ephemeral=True)

    @app_commands.command(name="edit-controller-tracker", description="Edit settings for an existing controller tracker.")
    @app_commands.describe(
        cid="The VATSIM CID of the controller tracker to edit.",
        channel="Change the channel (Optional).",
        role="Change the role to ping (Optional).",
        delete_on_offline="Change delete-on-offline behavior (Optional).",
        nickname="Change the nickname (Optional)."
    )
    @app_commands.check(check_manager_permissions)
    async def edit_controller_tracker(self, interaction: discord.Interaction, cid: str,
                                      channel: Optional[discord.TextChannel] = None,
                                      role: Optional[discord.Role] = None,
                                      delete_on_offline: Optional[bool] = None,
                                      nickname: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        # Strip 'CID: ' prefix if present (handles legacy data or user typos)
        cid = cid.replace('CID:', '').replace('CID ', '').strip()

        tracker = await self.db_manager.get_controller_tracker_by_cid(interaction.guild_id, cid)
        if not tracker:
            await interaction.followup.send(f"No controller tracker found for CID `{cid}`.", ephemeral=True)
            return

        tracker_id = tracker[0]

        # Build update parameters
        updates = []
        if channel is not None:
            await self.db_manager.update_controller_tracker(tracker_id, channel_id=channel.id)
            updates.append(f"Channel: {channel.mention}")
        if role is not None:
            await self.db_manager.update_controller_tracker(tracker_id, role_id=role.id)
            updates.append(f"Role: {role.mention}")
        if delete_on_offline is not None:
            await self.db_manager.update_controller_tracker(tracker_id, delete_on_offline=delete_on_offline)
            updates.append(f"Delete on offline: {delete_on_offline}")
        if nickname is not None:
            await self.db_manager.update_controller_tracker(tracker_id, nickname=nickname)
            updates.append(f"Nickname: {nickname}")

        if not updates:
            await interaction.followup.send("No changes specified. Please provide at least one parameter to update.", ephemeral=True)
            return

        updates_text = "\n• ".join(updates)
        await interaction.followup.send(f"✅ Updated controller tracker for CID `{cid}`:\n• {updates_text}", ephemeral=True)

    @edit_controller_tracker.autocomplete('cid')
    async def edit_controller_tracker_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        trackers = await self.db_manager.get_all_controller_trackers()
        guild_trackers = [t for t in trackers if t[1] == interaction.guild_id]

        choices = []
        for tracker in guild_trackers:
            # Handle both old and new format
            if len(tracker) == 9:
                cid = tracker[4]
                nickname = tracker[8]
            else:
                cid = tracker[4]
                nickname = None
            
            name = f"CID: {cid}"
            if nickname:
                name = f"{nickname} (CID: {cid})"
            
            choices.append(app_commands.Choice(name=name, value=str(cid)))

        if current:
            return [choice for choice in choices if current.lower() in choice.name.lower() or current.lower() in choice.value.lower()]
        
        return choices[:25]

    @untrack_controller.autocomplete('cid')
    async def untrack_controller_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        trackers = await self.db_manager.get_all_controller_trackers()
        guild_trackers = [t for t in trackers if t[1] == interaction.guild_id]

        choices = [
            app_commands.Choice(name=f"CID: {tracker[4]}", value=str(tracker[4]))
            for tracker in guild_trackers
        ]

        # Filter choices based on what the user is currently typing
        if current:
            return [choice for choice in choices if current.lower() in choice.value.lower()]
        
        return choices[:25] # limit of 25... Discord L

    @app_commands.command(name="list-controller-trackers", description="Lists all active controller trackers for this server.")
    async def list_controller_trackers(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        all_trackers = await self.db_manager.get_all_controller_trackers()
        guild_trackers = [t for t in all_trackers if t[1] == interaction.guild_id]
        
        if not guild_trackers:
            await interaction.followup.send("There are no active controller trackers for this server.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"Active Controller Trackers for {interaction.guild.name}",
            color=discord.Color.og_blurple()
        )
        
        description = ""
        for tracker in guild_trackers:
            # Handle both old (8 columns) and new (9 columns) format
            if len(tracker) == 9:
                tracker_id, guild_id, channel_id, message_id, cid, delete_on_offline, role_id, ping_sent, nickname = tracker
            else:
                tracker_id, guild_id, channel_id, message_id, cid, delete_on_offline, role_id, ping_sent = tracker
                nickname = None
            
            channel = self.bot.get_channel(channel_id)
            channel_text = channel.mention if channel else f"Unknown Channel (ID: {channel_id})"
            
            role = interaction.guild.get_role(role_id) if role_id else None
            role_text = f", pings {role.mention}" if role else ""
            
            delete_text = " 🗑️" if delete_on_offline else ""
            
            label = f"**CID `{cid}`**"
            if nickname:
                label = f"**{nickname}** (CID `{cid}`)"
            
            description += f"• {label} → {channel_text}{role_text}{delete_text}\n"
        
        embed.description = description
        embed.set_footer(text="🗑️ = Message deleted when offline")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- BACKGROUND LOOP FOR CONTROLLER TRACKING ---

    @tasks.loop(minutes=4)
    async def update_controller_trackers(self):
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        print(f"[{timestamp}] Running controller tracker update loop...")
        all_trackers = await self.db_manager.get_all_controller_trackers()
        if not all_trackers:
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
            print(f"[{timestamp}] No controller trackers found.")
            return

        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        print(f"[{timestamp}] Found {len(all_trackers)} controller tracker(s) to update.")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(VATSIM_DATA_URL) as response:
                    if response.status != 200:
                        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
                        print(f"[{timestamp}] Error fetching VATSIM data for controller trackers: Status {response.status}")
                        return
                    vatsim_data = await response.json()
        except aiohttp.ClientError as e:
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
            print(f"[{timestamp}] AIOHTTP Error fetching VATSIM data for controller trackers: {e}")
            return
            
        controllers_by_cid = {str(c['cid']): c for c in vatsim_data.get('controllers', [])}

        for tracker_data in all_trackers:
            # Handle both old (8 columns) and new (9 columns) format for backward compatibility
            if len(tracker_data) == 9:
                tracker_id, guild_id, channel_id, message_id, cid, delete_on_offline, role_id, ping_sent, nickname = tracker_data
            else:
                tracker_id, guild_id, channel_id, message_id, cid, delete_on_offline, role_id, ping_sent = tracker_data
                nickname = None
            
            channel = self.bot.get_channel(channel_id)
            if not channel:
                await self.db_manager.remove_controller_tracker(tracker_id)
                continue

            controller_data = controllers_by_cid.get(cid)

            if controller_data: # Controller is ONLINE
                embed = create_controller_embed(controller_data, nickname)
                content_to_send = None

                if role_id and not ping_sent:
                    guild = self.bot.get_guild(guild_id)
                    if guild:
                        role = guild.get_role(role_id)
                        if role:
                            content_to_send = role.mention
                    await self.db_manager.set_controller_tracker_ping_status(tracker_id, True)

                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                        await message.edit(content=content_to_send, embed=embed)
                        await asyncio.sleep(1) 
                    except discord.NotFound:
                        # Message was deleted, so we'll post a new one
                        new_message = await channel.send(content=content_to_send, embed=embed)
                        await self.db_manager.update_tracker_message(tracker_id, new_message.id)
                    except discord.Forbidden:
                        continue 
                else: # No message_id, means we need to post a new one
                    try:
                        new_message = await channel.send(content=content_to_send, embed=embed)
                        await self.db_manager.update_tracker_message(tracker_id, new_message.id)
                    except discord.Forbidden:
                        continue

            else: # Controller is OFFLINE
                if ping_sent:
                    await self.db_manager.set_controller_tracker_ping_status(tracker_id, False)

                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                        if delete_on_offline:
                            await message.delete()
                            await self.db_manager.clear_tracker_message(tracker_id)
                        else:
                            embed = discord.Embed(
                                title="📡 Controller Offline",
                                description=f"The controller with CID `{cid}` is not currently connected to VATSIM.",
                                color=discord.Color.red(),
                                timestamp=datetime.datetime.now(datetime.timezone.utc)
                            )
                            embed.set_footer(text="Last Updated")
                            await message.edit(content=None, embed=embed)
                            await asyncio.sleep(2)
                    except (discord.NotFound, discord.Forbidden):
                        # If we can't find or access the message, clear it from the DB
                        await self.db_manager.clear_tracker_message(tracker_id)

    @update_controller_trackers.before_loop
    async def before_update_controller_trackers(self):
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        print(f"[{timestamp}] Waiting for bot to be ready before starting controller tracker loop...")
        await self.bot.wait_until_ready()
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H:%M:%SZ")
        print(f"[{timestamp}] Bot ready. Controller tracker loop will now start.")

    # --- HELPER METHODS FOR CONTROLLER TRACKING ---
    
    async def update_specific_controller_tracker(self, message_id: int, channel_id: int, cid: str):
        """Manually triggers an update for a single controller tracker."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(VATSIM_DATA_URL) as response:
                    if response.status != 200: return
                    vatsim_data = await response.json()
        except aiohttp.ClientError:
            return

        controller_data = next((c for c in vatsim_data.get('controllers', []) if str(c['cid']) == cid), None)
        
        channel = self.bot.get_channel(channel_id)
        if not channel: return
        try:
            message = await channel.fetch_message(message_id)
            # Get nickname from guild
            tracker = await self.db_manager.get_controller_tracker_by_cid(message.guild.id, cid)
            nickname = tracker[8] if tracker and len(tracker) > 8 else None
            
            if controller_data:
                embed = create_controller_embed(controller_data, nickname)
                # We don't handle pings here since this is a manual, one-off update
                await message.edit(content=None, embed=embed)
            else:
                title = "📡 Controller Offline"
                description = f"The controller with CID `{cid}` is not currently connected to VATSIM."
                if nickname:
                    title = f"📡 {nickname} Offline"
                    description = f"**{nickname}** (CID `{cid}`) is not currently connected to VATSIM."
                    
                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=discord.Color.red(),
                    timestamp=datetime.datetime.now(datetime.timezone.utc)
                )
                embed.set_footer(text="Last Updated")
                await message.edit(content=None, embed=embed)
        except (discord.NotFound, discord.Forbidden):
            return

async def setup(bot: commands.Bot):
    await bot.add_cog(AtcCog(bot))

