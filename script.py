import discord
from discord import app_commands
import json
import aiohttp
import datetime
import os
import asyncio
from typing import Optional, List
import aiofiles
import io
from aiolimiter import AsyncLimiter
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

class BackupBot(discord.Client):
    """A specialized Discord client for creating and restoring server backups."""
    def __init__(self):
        intents = discord.Intents.all()  # Enable all intents
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

client = BackupBot()

def get_backup_files():
    """Return a list of backup JSON files found in the current directory."""
    files = []
    for f in os.listdir('.'):
        if f.startswith('backup_') and f.endswith('.json'):
            files.append(f)
    return sorted(files, reverse=True)

def get_guild_name(client, guild_id):
    guild = client.get_guild(int(guild_id))
    return guild.name if guild else f"Unknown Server ({guild_id})"

async def send_progress(interaction, message):
    try:
        await interaction.followup.send(message, ephemeral=True)
    except:
        pass

async def backup_role(role):
    """Backup an individual role's attributes."""
    logging.info(f"Backing up role: {role.name}")
    return {
        'name': role.name,
        'permissions': role.permissions.value,
        'color': role.color.value,
        'hoist': role.hoist,
        'mentionable': role.mentionable,
        'position': role.position,
        'id': role.id,
        'is_default': role.is_default()
    }

def serialize_overwrites(overwrites):
    serialized = {}
    # Always include @everyone permissions if they exist
    for target, overwrite in overwrites.items():
        if target.is_default():  # This is the @everyone role
            allowed, denied = overwrite.pair()
            serialized['everyone'] = {
                'type': 'role',
                'allow': allowed.value,
                'deny': denied.value
            }
            break  # Found @everyone, no need to continue this loop
    
    # Save other permission overwrites
    for target, overwrite in overwrites.items():
        if not target.is_default():  # Skip @everyone as we handled it above
            key = str(target.id)
            allowed, denied = overwrite.pair()
            serialized[key] = {
                'type': 'role' if isinstance(target, discord.Role) else 'member',
                'allow': allowed.value,
                'deny': denied.value
            }
    return serialized

async def backup_channel(channel, max_messages: Optional[int] = None):
    logging.info(f"Backing up channel: #{channel.name}")
    try:
        messages = []
        if isinstance(channel, discord.TextChannel):
            try:
                async for message in channel.history(limit=max_messages):
                    if message.content.strip().lower().startswith('/'):
                        continue
                        
                    username = str(message.author.name)
                    content = message.content
                    
                    # Convert mentions
                    for mention in message.mentions:
                        content = content.replace(f'<@{mention.id}>', f'@{mention.name}')
                    for mention in message.role_mentions:
                        content = content.replace(f'<@&{mention.id}>', f'@{mention.name}')
                    
                    messages.append({
                        'content': content,
                        'author': username,
                        'avatar_url': str(message.author.avatar.url) if message.author.avatar else None,
                        'attachments': [a.url for a in message.attachments],
                        'embeds': [embed.to_dict() for embed in message.embeds],
                        'timestamp': message.created_at.isoformat()
                    })
            except Exception as e:
                logging.error(f"Error backing up #{channel.name}: {str(e)}")
        
        # Get permission overwrites
        overwrites = serialize_overwrites(channel.overwrites)
        
        channel_data = {
            'name': channel.name,
            'type': str(channel.type),
            'topic': channel.topic if isinstance(channel, discord.TextChannel) else None,
            'position': channel.position,
            'nsfw': channel.nsfw if isinstance(channel, discord.TextChannel) else False,
            'slowmode_delay': channel.slowmode_delay if isinstance(channel, discord.TextChannel) else 0,
            'bitrate': channel.bitrate if isinstance(channel, discord.VoiceChannel) else None,
            'user_limit': channel.user_limit if isinstance(channel, discord.VoiceChannel) else None,
            'category_id': channel.category.id if channel.category else None,
            'category_name': str(channel.category) if channel.category else None,
            'permission_overwrites': overwrites,
            'messages': messages[::-1] if messages else []
        }
        
        logging.info(f"Successfully backed up {len(messages)} messages from #{channel.name}")
        return channel_data
    except Exception as e:
        logging.error(f"Error backing up channel {channel.name}: {e}")
        # Return basic channel data on error
        return {
            'name': channel.name,
            'type': str(channel.type),
            'messages': []
        }

async def backup_member_roles(member):
    return {
        'id': member.id,
        'name': str(member),
        'roles': [role.id for role in member.roles if not role.is_default()]
    }

async def download_asset(url):
    """Download asset from URL and return bytes"""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.read()
            return None

async def backup_emoji_or_sticker(asset):
    """Backup an emoji or sticker with proper image downloading"""
    try:
        logging.info(f"Backing up {'emoji' if isinstance(asset, discord.Emoji) else 'sticker'}: {asset.name}")
        # Get the image URL
        asset_url = asset.url
        
        # Download the image
        image_data = await download_asset(asset_url)
        if not image_data:
            logging.error(f"Failed to download {'emoji' if isinstance(asset, discord.Emoji) else 'sticker'}: {asset.name}")
            return None
            
        return {
            'name': asset.name,
            'url': str(asset_url),
            'image': image_data.hex(),  # Convert bytes to hex string
            'type': 'emoji' if isinstance(asset, discord.Emoji) else 'sticker'
        }
    except Exception as e:
        logging.error(f"Error backing up {'emoji' if isinstance(asset, discord.Emoji) else 'sticker'} {asset.name}: {e}")
        return None

async def backup_server(interaction, max_messages: Optional[int] = None):
    logging.info(f"\nStarting backup of server: {interaction.guild.name}")
    guild = interaction.guild
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_data = {
        'server_name': guild.name,
        'roles': {},
        'categories': {},
        'channels': {},
        'members': [],
        'bans': [],
        'emojis': [],
        'stickers': []
    }

    # Add ban backup section right after initial setup
    logging.info("Backing up bans...")
    try:
        async for ban_entry in guild.bans():
            backup_data['bans'].append({
                'user_id': ban_entry.user.id,
                'user_name': str(ban_entry.user),
                'reason': ban_entry.reason
            })
        logging.info(f"✓ Backed up {len(backup_data['bans'])} bans")
    except Exception as e:
        logging.error(f"Error backing up bans: {e}")

    # Removed automod backup section

    # Backup roles
    logging.info("Backing up roles...")
    for role in guild.roles:
        backup_data['roles'][role.id] = await backup_role(role)
    logging.info(f"✓ Backed up {len(backup_data['roles'])} roles")

    # Backup member roles
    logging.info("Backing up member roles...")
    member_tasks = []
    for member in guild.members:
        if not member.bot:  # Skip bots
            member_tasks.append(backup_member_roles(member))
    
    backup_data['members'] = await asyncio.gather(*member_tasks)
    logging.info(f"✓ Backed up roles for {len(backup_data['members'])} members")
    
    # Backup categories
    logging.info("Backing up categories...")
    for category in guild.categories:
        backup_data['categories'][category.id] = {
            'name': category.name,
            'position': category.position
        }
    logging.info(f"✓ Backed up {len(backup_data['categories'])} categories")
    
    # Backup channels
    channel_tasks = [backup_channel(channel, max_messages) 
                    for channel in guild.channels]
    channel_data = await asyncio.gather(*channel_tasks)
    
    # Add a message counter
    total_messages = 0
    channel_stats = {}
    for data in channel_data:
        backup_data['channels'][data['name']] = data
        count = len(data.get('messages', []))
        total_messages += count
        channel_stats[data['name']] = count
    
    # Backup emojis
    logging.info("Backing up emojis...")
    emoji_tasks = []
    for emoji in guild.emojis:
        if not emoji.managed:  # Skip managed emojis (like Nitro emojis)
            emoji_tasks.append(backup_emoji_or_sticker(emoji))
    
    if emoji_tasks:
        emoji_results = await asyncio.gather(*emoji_tasks)
        # Filter out None results from failed backups
        backup_data['emojis'] = [e for e in emoji_results if e is not None]
        logging.info(f"✓ Backed up {len(backup_data['emojis'])} emojis")
    else:
        logging.info("No emojis to backup")

    # Backup stickers
    logging.info("Backing up stickers...")
    sticker_tasks = []
    for sticker in guild.stickers:
        if not sticker.available:  # Skip unavailable stickers
            continue
        sticker_tasks.append(backup_emoji_or_sticker(sticker))
    
    if sticker_tasks:
        sticker_results = await asyncio.gather(*sticker_tasks)
        # Filter out None results from failed backups
        backup_data['stickers'] = [s for s in sticker_results if s is not None]
        logging.info(f"✓ Backed up {len(backup_data['stickers'])} stickers")
    else:
        logging.info("No stickers to backup")

    filename = f'backup_{interaction.guild.id}_{timestamp}.json'
    async with aiofiles.open(filename, 'w') as f:
        await f.write(json.dumps(backup_data))
    
    logging.info(f"Backup completed! Saved to {filename}")
    
    # Return necessary data for the backup summary
    return backup_data, channel_stats, timestamp

async def restore_channel(channel, data):
    """Restore messages into a newly created channel using a webhook."""
    logging.info(f"Restoring messages in #{channel.name}")
    webhook = await channel.create_webhook(name='RestoreBot')
    try:
        for msg in data['messages']:
            try:
                embeds = [
                    discord.Embed.from_dict(embed_data)
                    for embed_data in msg.get('embeds', [])
                    if embed_data
                ]
                
                content = msg['content']
                if not content and msg['attachments']:
                    content = '\n'.join(msg['attachments'])
                    
                if content or embeds or msg['attachments']:
                    retry_count = 0
                    while retry_count < 3:  # Maximum 3 retries per message
                        try:
                            await send_with_rate_limit(
                                webhook,
                                content=content,
                                username=msg['author'],
                                avatar_url=msg.get('avatar_url'),
                                embeds=embeds
                            )
                            break  # Message sent successfully
                        except discord.HTTPException as e:
                            if e.code == 429:  # Rate limit hit
                                retry_count += 1
                                retry_after = e.retry_after
                                # Add extra buffer to the retry time
                                await asyncio.sleep(retry_after + 1)
                            else:
                                raise  # Re-raise if it's not a rate limit error
            except Exception as e:
                logging.error(f"Error restoring message: {e}")
                await asyncio.sleep(5)  # Longer delay on error
                continue
    finally:
        await webhook.delete()
    logging.info(f"Restored {len(data['messages'])} messages in #{channel.name}")

async def restore_channels(interaction, backup, roles):
    logging.info("Restoring server structure...")
    
    # Create/update roles first
    logging.info("Restoring roles...")
    default_role = interaction.guild.default_role
    
    for role_id, role_data in backup.get('roles', {}).items():
        try:
            if role_data.get('is_default'):
                logging.info(f"Updating @everyone permissions")
                await default_role.edit(permissions=discord.Permissions(role_data['permissions']))
                roles[int(role_id)] = default_role
            else:
                new_name = f"{role_data['name']}-restored"
                logging.info(f"Creating role: {new_name}")
                new_role = await interaction.guild.create_role(
                    name=new_name,
                    permissions=discord.Permissions(role_data['permissions']),
                    color=discord.Color(role_data['color']),
                    hoist=role_data['hoist'],
                    mentionable=role_data['mentionable']
                )
                roles[int(role_id)] = new_role
                logging.info(f"✓ Created role: {new_name}")
        except Exception as e:
            logging.error(f"❌ Error creating role {role_data['name']}: {e}")
    
    # Wait for roles to be available
    await asyncio.sleep(2)
    
    # Create categories first
    logging.info("Restoring categories...")
    categories = {}
    for cat_id, cat_data in backup.get('categories', {}).items():
        try:
            category = await interaction.guild.create_category(
                name=f"{cat_data['name']}-restored",
                position=cat_data['position']
            )
            categories[int(cat_id)] = category
        except Exception as e:
            logging.error(f"Error creating category {cat_data['name']}: {e}")
    logging.info(f"✓ Restored {len(categories)} categories")

    # Create all channels
    logging.info("Restoring channels...")
    channels = []
    for channel_name, channel_data in backup['channels'].items():
        try:
            new_name = f"{channel_name}-restored"
            category = None
            if channel_data.get('category_id'):
                category = categories.get(int(channel_data['category_id']))
            
            # Convert permission overwrites
            overwrites = {}
            for target_id, overwrite_data in channel_data.get('permission_overwrites', {}).items():
                if target_id == 'everyone':
                    target = interaction.guild.default_role
                else:
                    target = roles.get(int(target_id)) or interaction.guild.get_role(int(target_id))
                
                if target:
                    allow = discord.Permissions(overwrite_data['allow'])
                    deny = discord.Permissions(overwrite_data['deny'])
                    overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)

            if channel_data['type'] == 'text':
                new_channel = await interaction.guild.create_text_channel(
                    name=new_name,
                    topic=channel_data.get('topic'),
                    position=channel_data.get('position', 0),
                    nsfw=channel_data.get('nsfw', False),
                    slowmode_delay=channel_data.get('slowmode_delay', 0),
                    category=category,
                    overwrites=overwrites
                )
                channels.append((new_channel, channel_data))
            elif channel_data['type'] == 'voice':
                await interaction.guild.create_voice_channel(
                    name=new_name,
                    bitrate=channel_data.get('bitrate', 64000),
                    user_limit=channel_data.get('user_limit', 0),
                    position=channel_data.get('position', 0),
                    category=category,
                    overwrites=overwrites
                )

        except Exception as e:
            await send_progress(interaction, f"Error creating channel {channel_name}: {str(e)}")
            continue
    logging.info(f"✓ Restored {len(channels)} channels")

    return channels

async def restore_server(interaction, backup):
    logging.info(f"\nStarting restore for server: {interaction.guild.name}")
    
    # Create/update roles first
    logging.info("Restoring roles...")
    roles = {}
    default_role = interaction.guild.default_role
    
    for role_id, role_data in backup.get('roles', {}).items():
        try:
            if role_data.get('is_default'):
                logging.info(f"Updating @everyone permissions")
                await default_role.edit(permissions=discord.Permissions(role_data['permissions']))
                roles[int(role_id)] = default_role
            else:
                new_name = f"{role_data['name']}-restored"
                logging.info(f"Creating role: {new_name}")
                new_role = await interaction.guild.create_role(
                    name=new_name,
                    permissions=discord.Permissions(role_data['permissions']),
                    color=discord.Color(role_data['color']),
                    hoist=role_data['hoist'],
                    mentionable=role_data['mentionable']
                )
                roles[int(role_id)] = new_role
                logging.info(f"✓ Created role: {new_name}")
        except Exception as e:
            logging.error(f"❌ Error creating role {role_data['name']}: {e}")
    
    # Wait for roles to be available
    await asyncio.sleep(2)
    
    # Restore member roles
    logging.info("Restoring member roles...")
    for member_data in backup.get('members', []):
        try:
            logging.info(f"Restoring roles for member: {member_data['name']}")
            member = interaction.guild.get_member(member_data['id'])
            if member:
                role_ids = member_data['roles']
                restored_roles = [roles.get(role_id) for role_id in role_ids if roles.get(role_id)]
                if restored_roles:
                    await member.add_roles(*restored_roles, reason="Backup restoration")
                    logging.info(f"✓ Restored {len(restored_roles)} roles for: {member_data['name']}")
        except Exception as e:
            logging.error(f"❌ Error restoring roles for {member_data['name']}: {e}")
    
    # Continue with rest of restoration - bans, emojis, stickers
    for ban_data in backup.get('bans', []):
        try:
            user = await client.fetch_user(ban_data['user_id'])
            await interaction.guild.ban(user, reason=f"Restored ban: {ban_data['reason']}")
        except Exception as e:
            logging.error(f"Error restoring ban for user {ban_data['user_id']}: {e}")
    
    # Restore emojis and stickers
    for emoji_data in backup.get('emojis', []):
        try:
            image = bytes.fromhex(emoji_data['image'])
            await interaction.guild.create_custom_emoji(
                name=f"{emoji_data['name']}-restored",
                image=image
            )
        except Exception as e:
            logging.error(f"Error restoring emoji {emoji_data['name']}: {e}")
    
    for sticker_data in backup.get('stickers', []):
        try:
            image = bytes.fromhex(sticker_data['image'])
            await interaction.guild.create_sticker(
                name=f"{sticker_data['name']}-restored",
                file=discord.File(io.BytesIO(image), filename='sticker.png'),
                emoji='👍'  # Default emoji
            )
        except Exception as e:
            logging.error(f"Error restoring sticker {sticker_data['name']}: {e}")

    # Note: roles dict is now passed to restore_channels
    return await restore_channels(interaction, backup, roles)

@client.tree.command(name="backup", description="Create a backup of the entire server")
@app_commands.describe(
    max_messages="Maximum number of messages to backup per channel (default: all messages)"
)
@app_commands.checks.has_permissions(administrator=True)
async def backup(interaction: discord.Interaction, max_messages: Optional[int] = None):
    await interaction.response.defer(ephemeral=True)
    await send_progress(interaction, "Starting comprehensive server backup...")
    
    backup_data, channel_stats, timestamp = await backup_server(interaction, max_messages)
    
    await send_progress(interaction, "Backing up bans...")
    await send_progress(interaction, "Backing up roles...")
    await send_progress(interaction, "Backing up member roles...")
    await send_progress(interaction, "Backing up categories...")
    await send_progress(interaction, "Backing up channels... This may take a while.")
    await send_progress(interaction, "Backing up emojis and stickers...")

    # Construct a single, compact backup summary message
    # Sort channels by message count and get top 5
    top_channels = sorted(channel_stats.items(), key=lambda x: x[1], reverse=True)[:5]
    
    stats_message = (
        f"**Backup Completed!**\n"
        f"• Roles: {len(backup_data['roles'])}\n"
        f"• Categories: {len(backup_data['categories'])}\n"
        f"• Channels: {len(backup_data['channels'])}\n"
        f"• Messages: {sum(channel_stats.values())}\n"
        f"• Emojis: {len(backup_data['emojis'])}\n"
        f"• Stickers: {len(backup_data['stickers'])}\n"
        f"• Bans: {len(backup_data['bans'])}\n"
        # Removed AutoMod Rules line
        f"• Backup ID: {timestamp}\n\n"
        f"**Top 5 Channels:**\n" + " ".join(
            [f"• #{channel}: {count} msgs\n" for channel, count in top_channels]
        )
    )
    
    await interaction.followup.send(stats_message, ephemeral=True)

@client.tree.command(name="restore", description="Restore a backup (creates new channels)")
@app_commands.describe(
    backup_id="The backup ID to restore (format: YYYYMMDD_HHMMSS)",
    force="Bypass server ID check to restore from another server backup"
)
@app_commands.checks.has_permissions(administrator=True)
async def restore(interaction: discord.Interaction, backup_id: str, force: bool = False):
    await interaction.response.defer(ephemeral=True)
    
    # Get backup file
    backup_files = get_backup_files()
    filename = next((f for f in backup_files if backup_id in f), None)
    
    # Check if backup belongs to this server
    backup_server_id = filename.split('_')[1] if filename else None
    if backup_server_id and not force and backup_server_id != str(interaction.guild.id):
        await interaction.followup.send(
            "Backup belongs to a different server. Use force=True to override.",
            ephemeral=True
        )
        return
    
    if not filename:
        await interaction.followup.send(
            "Backup ID not found! Use `/backups` to see available backups.",
            ephemeral=True
        )
        return
    
    try:
        with open(filename, 'r') as f:
            backup = json.load(f)
        
        await send_progress(interaction, "Restoring roles...")
        roles = {}
        # Create categories and channels
        channels = await restore_server(interaction, backup)
        
        await send_progress(interaction, "Restoring member roles...")
        # Restore messages sequentially to avoid rate limits
        total_restored = 0
        for channel, data in channels:
            if (data['type'] == 'text'):
                await restore_channel(channel, data)
                total_restored += 1
                await send_progress(interaction, f"Restored messages in {total_restored}/{len(channels)} channels")
            
        await interaction.followup.send(
            f"Restore completed!\n"
            f"• Categories restored: {len(backup.get('categories', {}))}\n"
            f"• Channels restored: {len(channels)}",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"Error during restore: {str(e)}", ephemeral=True)

@client.tree.command(name="undo", description="Remove all restored channels and roles")
@app_commands.checks.has_permissions(administrator=True)
async def undo(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # Find restored channels
    restored_channels = [c for c in interaction.guild.channels if '-restored' in c.name]
    
    # Find restored roles
    restored_roles = [r for r in interaction.guild.roles if '-restored' in r.name]
    
    # Delete channels first
    for channel in restored_channels:
        try:
            await channel.delete()
        except Exception as e:
            logging.error(f"Error deleting channel {channel.name}: {e}")
    
    # Delete roles next
    for role in restored_roles:
        try:
            await role.delete()
        except Exception as e:
            logging.error(f"Error deleting role {role.name}: {e}")
    
    await interaction.followup.send(
        f"Cleanup completed!\n"
        f"• Channels removed: {len(restored_channels)}\n"
        f"• Roles removed: {len(restored_roles)}",
        ephemeral=True
    )

@client.tree.command(name="backups", description="List all available backups from all servers")
@app_commands.checks.has_permissions(administrator=True)
async def list_backups(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    backup_files = get_backup_files()
    
    if not backup_files:
        await interaction.followup.send("No backups found!", ephemeral=True)
        return
    
    # Group backups by server
    servers = {}
    for file in backup_files:
        try:
            # Extract guild ID from filename
            guild_id = file.split('_')[1]
            
            if guild_id not in servers:
                servers[guild_id] = []
            
            with open(file, 'r') as f:
                data = json.load(f)
            total_messages = sum(len(ch['messages']) for ch in data['channels'].values())
            
            # Parse timestamp
            parts = file.split('_')
            if len(parts) >= 4:
                date_part = parts[2]
                time_part = parts[3].split('.')[0]
                timestamp = f"{date_part}_{time_part}"
            else:
                timestamp = parts[2].split('.')[0]
            
            try:
                dt = datetime.datetime.strptime(timestamp, "%Y%m%d_%H%M%S")
                unix_ts = int(dt.timestamp())
                
                backup_info = {
                    'id': timestamp,
                    'unix_ts': unix_ts,
                    'channels': len(data['channels']),
                    'messages': total_messages
                }
                servers[guild_id].append(backup_info)
                
            except ValueError:
                continue
                
        except Exception as e:
            logging.error(f"Error processing backup file {file}: {e}")
            continue
    
    # Build output message
    output = ["__Available backups:__\n"]
    
    for guild_id, backups in servers.items():
        guild_name = get_guild_name(client, guild_id)
        output.append(f"\n**{guild_name}** (ID: {guild_id})")
        
        for backup in backups:
            output.append(
                f"┌ **ID: `{backup['id']}`**\n"
                f"├ Created: <t:{backup['unix_ts']}:R>\n"
                f"├ Full date: <t:{backup['unix_ts']}:F>\n"
                f"├ Channels: {backup['channels']}\n"
                f"└ Messages: {backup['messages']}\n"
            )
    
    # Split message if too long
    message = "\n".join(output)
    if len(message) > 2000:
        chunks = []
        current_chunk = [output[0]]
        current_length = len(output[0])
        
        for line in output[1:]:
            if current_length + len(line) + 1 > 1900:
                chunks.append("\n".join(current_chunk))
                current_chunk = [output[0]]
                current_length = len(output[0])
            current_chunk.append(line)
            current_length += len(line) + 1
        
        if current_chunk:
            chunks.append("\n".join(current_chunk))
            
        for i, chunk in enumerate(chunks):
            await interaction.followup.send(
                f"{chunk}\n\nPage {i+1}/{len(chunks)}", 
                ephemeral=True
            )
    else:
        await interaction.followup.send(message, ephemeral=True)

def find_backup_file(backup_id: str) -> Optional[str]:
    """Find a backup file by its ID"""
    backup_files = get_backup_files()
    return next((f for f in backup_files if backup_id in f), None)

@client.tree.command(name="delete", description="Delete a specific backup")
@app_commands.describe(backup_id="The backup ID to delete (format: YYYYMMDD_HHMMSS)")
@app_commands.checks.has_permissions(administrator=True)
async def delete_backup(interaction: discord.Interaction, backup_id: str):
    await interaction.response.defer(ephemeral=True)
    
    filename = find_backup_file(backup_id)
    if not filename:
        await interaction.followup.send(
            "Backup ID not found! Use `/backups` to see available backups.",
            ephemeral=True
        )
        return
    
    try:
        # Get backup info before deleting
        with open(filename, 'r') as f:
            data = json.load(f)
        
        guild_id = filename.split('_')[1]
        total_messages = sum(len(ch['messages']) for ch in data['channels'].values())
        total_channels = len(data['channels'])
        
        # Delete the file
        os.remove(filename)
        
        await interaction.followup.send(
            f"Backup deleted successfully!\n"
            f"• Backup ID: `{backup_id}`\n"
            f"• Server ID: {guild_id}\n"
            f"• Channels: {total_channels}\n"
            f"• Messages: {total_messages}",
            ephemeral=True
        )
    except FileNotFoundError:
        await interaction.followup.send(
            "Backup file not found! It may have been already deleted.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"Error deleting backup: {str(e)}",
            ephemeral=True
        )

@client.tree.command(name="remove_restored_prefix", description="Remove the '-restored' prefix from all restored channels and roles")
@app_commands.checks.has_permissions(administrator=True)
async def remove_restored_prefix(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # Find restored channels
    restored_channels = [c for c in interaction.guild.channels if c.name.endswith('-restored')]
    
    # Find restored roles
    restored_roles = [r for r in interaction.guild.roles if r.name.endswith('-restored')]
    
    # Rename channels
    for channel in restored_channels:
        try:
            new_name = channel.name.replace('-restored', '')
            await channel.edit(name=new_name)
        except Exception as e:
            logging.error(f"Error renaming channel {channel.name}: {e}")
    
    # Rename roles
    for role in restored_roles:
        try:
            new_name = role.name.replace('-restored', '')
            await role.edit(name=new_name)
        except Exception as e:
            logging.error(f"Error renaming role {role.name}: {e}")
    
    await interaction.followup.send(
        f"Renaming completed!\n"
        f"• Channels renamed: {len(restored_channels)}\n"
        f"• Roles renamed: {len(restored_roles)}",
        ephemeral=True
    )

@client.tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need administrator permissions to use this command!", 
                ephemeral=True
            )
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"An error occurred: {str(error)}", 
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"An error occurred: {str(error)}", 
                    ephemeral=True
                )
    except Exception as e:
        logging.error(f"Error in error handler: {e}")

# Create a rate limiter for Discord API 
rate_limiter = AsyncLimiter(1, 1)

async def send_with_rate_limit(webhook, content, username, avatar_url, embeds):
    async with rate_limiter:
        await webhook.send(
            content=content,
            username=username,
            avatar_url=avatar_url,
            embeds=embeds
        )

client.run('')
