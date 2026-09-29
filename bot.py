import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from aiohttp import web

# ─── Configuration ────────────────────────────────────────────────────────────
DATA_FILE = "data.json"

# ─── Chargement / Sauvegarde des données ──────────────────────────────────────
def load_data() -> dict:
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_user_data(data: dict, guild_id: int, user_id: int) -> dict:
    g = str(guild_id)
    u = str(user_id)
    data.setdefault(g, {})
    data[g].setdefault(u, {"avertissements": 0, "timeout_jusqu_a": None})
    return data[g][u]

# ─── Intent setup ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── Variables de configuration runtime (par serveur) ─────────────────────────
# guild_id -> { "channel_id": int, "admin_role_id": int }
guild_config: dict[int, dict] = {}

def load_guild_config():
    if Path("guild_config.json").exists():
        with open("guild_config.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
            for k, v in raw.items():
                guild_config[int(k)] = v

def save_guild_config():
    with open("guild_config.json", "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in guild_config.items()}, f, indent=2)

# ─── Vérification admin ────────────────────────────────────────────────────────
def est_admin(member: discord.Member, guild_id: int) -> bool:
    if member.guild_permissions.administrator:
        return True
    cfg = guild_config.get(guild_id, {})
    role_id = cfg.get("admin_role_id")
    if role_id:
        return any(r.id == role_id for r in member.roles)
    return False

def est_dans_salon_selfie(message: discord.Message) -> bool:
    cfg = guild_config.get(message.guild.id, {})
    channel_id = cfg.get("channel_id")
    return channel_id is not None and message.channel.id == channel_id

# ─── Événement : bot prêt ──────────────────────────────────────────────────────
@bot.event
async def on_ready():
    load_guild_config()
    print(f"✅ Bot connecté en tant que {bot.user} (ID: {bot.user.id})")
    try:
        synced = await tree.sync()
        print(f"✅ {len(synced)} commande(s) slash synchronisée(s).")
    except Exception as e:
        print(f"❌ Erreur de synchronisation : {e}")

# ─── Événement : message reçu ─────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    # Ignorer les messages du bot lui-même
    if message.author.bot:
        return

    # Ignorer les messages hors serveur
    if not message.guild:
        return

    # Si c'est dans le salon selfie configuré
    if est_dans_salon_selfie(message):

        # Les admins sont ignorés (leurs posts ne sont pas vérifiés)
        if est_admin(message.author, message.guild.id):
            # Ouvrir un thread quand même si l'admin poste une image
            if message.attachments and any(
                a.content_type and a.content_type.startswith("image/")
                for a in message.attachments
            ):
                await ouvrir_thread(message)
            return

        # Vérifier que le message contient AU MOINS une image
        contient_image = message.attachments and any(
            a.content_type and a.content_type.startswith("image/")
            for a in message.attachments
        )

        if not contient_image:
            await appliquer_avertissement(message)
        else:
            # ✅ Message valide → ouvrir un thread
            await ouvrir_thread(message)

    await bot.process_commands(message)

# ─── Ouvrir un thread sous le message ─────────────────────────────────────────
async def ouvrir_thread(message: discord.Message):
    try:
        nom_thread = f"💬 {message.author.display_name}"
        await message.create_thread(name=nom_thread, auto_archive_duration=1440)
    except discord.Forbidden:
        print(f"⚠️ Impossible de créer un thread (permissions manquantes).")
    except Exception as e:
        print(f"⚠️ Erreur création thread : {e}")

# ─── Appliquer un avertissement ───────────────────────────────────────────────
async def appliquer_avertissement(message: discord.Message):
    data = load_data()
    user_data = get_user_data(data, message.guild.id, message.author.id)

    # Supprimer le message invalide
    try:
        await message.delete()
    except discord.Forbidden:
        pass

    user_data["avertissements"] += 1
    nb = user_data["avertissements"]
    save_data(data)

    try:
        if nb < 3:
            # Envoyer un DM d'avertissement
            embed = discord.Embed(
                title="⚠️ Avertissement",
                description=(
                    f"Ton message dans **{message.guild.name}** a été supprimé.\n\n"
                    f"📸 Ce salon est **réservé aux selfies** : chaque message **doit** contenir une photo.\n\n"
                    f"🔢 Avertissement **{nb}/3**.\n"
                    f"Au 3ᵉ avertissement, tu seras mis en sourdine pendant **6 heures**."
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Serveur : {message.guild.name}")
            await message.author.send(embed=embed)
        else:
            # 3ᵉ avertissement → timeout 6 heures
            fin_timeout = datetime.now(timezone.utc) + timedelta(hours=6)
            user_data["timeout_jusqu_a"] = fin_timeout.isoformat()
            user_data["avertissements"] = 0  # remise à zéro après sanction
            save_data(data)

            try:
                await message.author.timeout(
                    timedelta(hours=6),
                    reason="3 avertissements dans le salon selfie"
                )
            except discord.Forbidden:
                pass

            embed = discord.Embed(
                title="🔇 Mise en sourdine",
                description=(
                    f"Tu as reçu **3 avertissements** dans **{message.guild.name}**.\n\n"
                    f"Tu es maintenant **mis en sourdine pendant 6 heures**.\n"
                    f"Rappel : le salon selfie exige une **photo dans chaque message**."
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Sourdine levée le {fin_timeout.strftime('%d/%m/%Y à %H:%M')} UTC")
            await message.author.send(embed=embed)

    except discord.Forbidden:
        # L'utilisateur a bloqué les DMs → on ne peut pas l'avertir en MP
        pass

# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDES SLASH (admins uniquement)
# ═══════════════════════════════════════════════════════════════════════════════

def verif_admin():
    """Décorateur de vérification admin pour les slash commands."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not est_admin(interaction.user, interaction.guild_id):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission d'utiliser cette commande.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# ─── /configurer ──────────────────────────────────────────────────────────────
@tree.command(name="configurer", description="⚙️ Configurer le salon selfie et le rôle admin.")
@verif_admin()
@app_commands.describe(
    salon="Le salon réservé aux selfies",
    role_admin="Le rôle considéré comme administrateur (optionnel)"
)
async def configurer(
    interaction: discord.Interaction,
    salon: discord.TextChannel,
    role_admin: discord.Role = None
):
    guild_config[interaction.guild_id] = {
        "channel_id": salon.id,
        "admin_role_id": role_admin.id if role_admin else None
    }
    save_guild_config()

    description = f"✅ Salon selfie configuré : {salon.mention}\n"
    if role_admin:
        description += f"👑 Rôle admin : {role_admin.mention}"
    else:
        description += "👑 Rôle admin : administrateurs serveur uniquement"

    await interaction.response.send_message(description, ephemeral=True)

# ─── /statut ──────────────────────────────────────────────────────────────────
@tree.command(name="statut", description="📊 Voir la configuration actuelle du bot.")
@verif_admin()
async def statut(interaction: discord.Interaction):
    cfg = guild_config.get(interaction.guild_id, {})
    channel_id = cfg.get("channel_id")
    role_id = cfg.get("admin_role_id")

    salon_txt = f"<#{channel_id}>" if channel_id else "❌ Non configuré"
    role_txt = f"<@&{role_id}>" if role_id else "Admins serveur uniquement"

    embed = discord.Embed(
        title="⚙️ Configuration du bot Selfie",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="📸 Salon selfie", value=salon_txt, inline=False)
    embed.add_field(name="👑 Rôle admin", value=role_txt, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── /avertissements ──────────────────────────────────────────────────────────
@tree.command(name="avertissements", description="🔍 Voir les avertissements d'un membre.")
@verif_admin()
@app_commands.describe(membre="Le membre à vérifier")
async def voir_avertissements(interaction: discord.Interaction, membre: discord.Member):
    data = load_data()
    user_data = get_user_data(data, interaction.guild_id, membre.id)
    nb = user_data.get("avertissements", 0)

    embed = discord.Embed(
        title=f"⚠️ Avertissements de {membre.display_name}",
        description=f"🔢 **{nb}/3** avertissement(s)",
        color=discord.Color.orange() if nb > 0 else discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── /reinitialiser ───────────────────────────────────────────────────────────
@tree.command(name="reinitialiser", description="🔄 Remettre à zéro les avertissements d'un membre.")
@verif_admin()
@app_commands.describe(membre="Le membre à réinitialiser")
async def reinitialiser(interaction: discord.Interaction, membre: discord.Member):
    data = load_data()
    g = str(interaction.guild_id)
    u = str(membre.id)
    if g in data and u in data[g]:
        data[g][u]["avertissements"] = 0
        data[g][u]["timeout_jusqu_a"] = None
        save_data(data)

    await interaction.response.send_message(
        f"✅ Avertissements de **{membre.display_name}** remis à zéro.",
        ephemeral=True
    )

# ─── /forcer_avertissement ─────────────────────────────────────────────────────
@tree.command(name="forcer_avertissement", description="⚠️ Donner manuellement un avertissement à un membre.")
@verif_admin()
@app_commands.describe(membre="Le membre à avertir", raison="Raison de l'avertissement")
async def forcer_avertissement(
    interaction: discord.Interaction,
    membre: discord.Member,
    raison: str = "Non spécifiée"
):
    data = load_data()
    user_data = get_user_data(data, interaction.guild_id, membre.id)
    user_data["avertissements"] += 1
    nb = user_data["avertissements"]
    save_data(data)

    if nb >= 3:
        fin_timeout = datetime.now(timezone.utc) + timedelta(hours=6)
        user_data["avertissements"] = 0
        user_data["timeout_jusqu_a"] = fin_timeout.isoformat()
        save_data(data)
        try:
            await membre.timeout(timedelta(hours=6), reason=raison)
        except discord.Forbidden:
            pass
        await interaction.response.send_message(
            f"🔇 **{membre.display_name}** a atteint 3 avertissements → mis en sourdine 6h. Raison : {raison}",
            ephemeral=True
        )
        try:
            embed = discord.Embed(
                title="🔇 Mise en sourdine",
                description=f"Tu as été mis en sourdine 6h sur **{interaction.guild.name}**.\nRaison : {raison}",
                color=discord.Color.red()
            )
            await membre.send(embed=embed)
        except discord.Forbidden:
            pass
    else:
        await interaction.response.send_message(
            f"⚠️ Avertissement **{nb}/3** ajouté à **{membre.display_name}**. Raison : {raison}",
            ephemeral=True
        )
        try:
            embed = discord.Embed(
                title="⚠️ Avertissement",
                description=f"Tu as reçu un avertissement sur **{interaction.guild.name}**.\nRaison : {raison}\nAvertissements : **{nb}/3**",
                color=discord.Color.orange()
            )
            await membre.send(embed=embed)
        except discord.Forbidden:
            pass

# ─── Serveur HTTP keep-alive (pour Render + cron-job.org) ────────────────────
async def handle_ping(request):
    """Endpoint /ping appelé par cron-job.org pour garder Render éveillé."""
    return web.Response(text="✅ SelfieBot opérationnel", status=200)

async def demarrer_serveur_http():
    app_http = web.Application()
    app_http.router.add_get("/", handle_ping)
    app_http.router.add_get("/ping", handle_ping)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app_http)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Serveur HTTP keep-alive démarré sur le port {port}")

# ─── Lancement ────────────────────────────────────────────────────────────────
async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("❌ Variable d'environnement DISCORD_TOKEN manquante.")
    await demarrer_serveur_http()
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
