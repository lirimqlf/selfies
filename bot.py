import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from aiohttp import web
from cryptography.fernet import Fernet, MultiFernet, InvalidToken

# ─── Configuration ────────────────────────────────────────────────────────────
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_FILE = DATA_DIR / "data.enc"              # avertissements (chiffré)
CONFIG_FILE = DATA_DIR / "guild_config.enc"    # config des serveurs (chiffré)
RETENTION_JOURS = 90                           # purge auto après inactivité
MAX_AVERT = 3
PRIVACY_URL = os.getenv("PRIVACY_URL", "https://VOTRE-PSEUDO.github.io/selfies/privacy.html")
TERMS_URL = os.getenv("TERMS_URL", "https://VOTRE-PSEUDO.github.io/selfies/terms.html")

data_lock = asyncio.Lock()

# ─── Chiffrement au repos (ToS Discord §5c) ───────────────────────────────────
_fernet: MultiFernet | None = None

def init_crypto():
    """DATA_ENCRYPTION_KEY : une ou plusieurs clés Fernet séparées par des virgules
    (la 1re chiffre, toutes déchiffrent → rotation de clé possible)."""
    global _fernet
    raw = os.getenv("DATA_ENCRYPTION_KEY", "")
    keys = [Fernet(k.strip().encode()) for k in raw.split(",") if k.strip()]
    if not keys:
        raise ValueError("❌ Variable d'environnement DATA_ENCRYPTION_KEY manquante.")
    _fernet = MultiFernet(keys)

def _ecrire_chiffre(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = _fernet.encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)  # écriture atomique

def _lire_chiffre(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(_fernet.decrypt(path.read_bytes()))
    except InvalidToken:
        raise RuntimeError(f"Impossible de déchiffrer {path} : mauvaise DATA_ENCRYPTION_KEY ?")

def maintenant() -> str:
    return datetime.now(timezone.utc).isoformat()

def migrer_ancien_stockage():
    """Chiffre les anciens data.json / guild_config.json (clair) puis les supprime."""
    ancien = DATA_DIR / "data.json"
    if ancien.exists() and not DATA_FILE.exists():
        brut = json.loads(ancien.read_text(encoding="utf-8"))
        data = {g: {u: {"avertissements": e.get("avertissements", 0), "maj": maintenant()}
                    for u, e in users.items()} for g, users in brut.items()}
        _ecrire_chiffre(DATA_FILE, data)
        ancien.unlink()
        print("🔐 data.json migré vers data.enc (chiffré).")
    ancien = DATA_DIR / "guild_config.json"
    if ancien.exists() and not CONFIG_FILE.exists():
        _ecrire_chiffre(CONFIG_FILE, json.loads(ancien.read_text(encoding="utf-8")))
        ancien.unlink()
        print("🔐 guild_config.json migré vers guild_config.enc (chiffré).")

# ─── Données (minimisées) : {guild_id: {user_id: {avertissements, maj}}} ──────
def load_data() -> dict:
    return _lire_chiffre(DATA_FILE)

def save_data(data: dict):
    _ecrire_chiffre(DATA_FILE, data)

def get_user_data(data: dict, guild_id: int, user_id: int) -> dict:
    return data.setdefault(str(guild_id), {}).setdefault(
        str(user_id), {"avertissements": 0, "maj": maintenant()})

def retirer_utilisateur(data: dict, user_id: int, guild_id: int | None = None):
    """Supprime l'utilisateur d'un serveur (ou de tous si guild_id est None)."""
    for g in list(data):
        if guild_id is None or g == str(guild_id):
            data[g].pop(str(user_id), None)
            if not data[g]:
                del data[g]

def purger(data: dict):
    """Supprime les entrées vides ou inactives depuis plus de RETENTION_JOURS."""
    limite = datetime.now(timezone.utc) - timedelta(days=RETENTION_JOURS)
    for g in list(data):
        for u in list(data[g]):
            e = data[g][u]
            try:
                maj = datetime.fromisoformat(e["maj"])
            except (KeyError, ValueError, TypeError):
                maj = None
            if e.get("avertissements", 0) <= 0 or maj is None or maj < limite:
                del data[g][u]
        if not data[g]:
            del data[g]

async def ajouter_avertissement(guild_id: int, user_id: int) -> int:
    async with data_lock:
        data = load_data()
        u = get_user_data(data, guild_id, user_id)
        u["avertissements"] += 1
        u["maj"] = maintenant()
        nb = u["avertissements"]
        if nb >= MAX_AVERT:  # sanction → on ne conserve plus rien
            retirer_utilisateur(data, user_id, guild_id)
        save_data(data)
    return nb

# ─── Intents : aucun intent privilégié nécessaire (minimisation) ──────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
tree = bot.tree

# ─── Config par serveur : { guild_id: {"channel_id", "admin_role_id"} } ───────
guild_config: dict[int, dict] = {}

def load_guild_config():
    guild_config.clear()
    for k, v in _lire_chiffre(CONFIG_FILE).items():
        guild_config[int(k)] = v

def save_guild_config():
    _ecrire_chiffre(CONFIG_FILE, {str(k): v for k, v in guild_config.items()})

def est_admin(member: discord.Member, guild_id: int) -> bool:
    if member.guild_permissions.administrator:
        return True
    role_id = guild_config.get(guild_id, {}).get("admin_role_id")
    return bool(role_id) and any(r.id == role_id for r in member.roles)

def est_dans_salon_selfie(message: discord.Message) -> bool:
    channel_id = guild_config.get(message.guild.id, {}).get("channel_id")
    return channel_id is not None and message.channel.id == channel_id

def a_une_image(message: discord.Message) -> bool:
    return any(a.content_type and a.content_type.startswith("image/") for a in message.attachments)

# ─── Événements ───────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Bot connecté en tant que {bot.user} (ID: {bot.user.id})")
    if not purge_task.is_running():
        purge_task.start()
    try:
        synced = await tree.sync()
        print(f"✅ {len(synced)} commande(s) slash synchronisée(s).")
    except Exception as e:
        print(f"❌ Erreur de synchronisation : {e}")

@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Bot retiré d'un serveur → suppression de toutes ses données (ToS §5b)."""
    async with data_lock:
        data = load_data()
        data.pop(str(guild.id), None)
        save_data(data)
    if guild_config.pop(guild.id, None) is not None:
        save_guild_config()

@tasks.loop(hours=24)
async def purge_task():
    async with data_lock:
        data = load_data()
        purger(data)
        save_data(data)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if est_dans_salon_selfie(message):
        if est_admin(message.author, message.guild.id):
            if a_une_image(message):
                await ouvrir_thread(message)
            return
        if a_une_image(message):
            await ouvrir_thread(message)
        else:
            await appliquer_avertissement(message)

    await bot.process_commands(message)

async def ouvrir_thread(message: discord.Message):
    try:
        await message.create_thread(name=f"💬 {message.author.display_name}", auto_archive_duration=1440)
    except discord.Forbidden:
        print("⚠️ Impossible de créer un thread (permissions manquantes).")
    except Exception as e:
        print(f"⚠️ Erreur création thread : {e}")

async def appliquer_avertissement(message: discord.Message):
    try:
        await message.delete()
    except discord.Forbidden:
        pass

    nb = await ajouter_avertissement(message.guild.id, message.author.id)

    try:
        if nb < MAX_AVERT:
            embed = discord.Embed(
                title="⚠️ Avertissement",
                description=(
                    f"Ton message dans **{message.guild.name}** a été supprimé.\n\n"
                    f"📸 Ce salon est **réservé aux selfies** : chaque message **doit** contenir une photo.\n\n"
                    f"🔢 Avertissement **{nb}/{MAX_AVERT}**.\n"
                    f"Au {MAX_AVERT}ᵉ avertissement, tu seras mis en sourdine pendant **6 heures**.\n\n"
                    f"🔐 Tes données : /mes_donnees · /supprimer_mes_donnees · {PRIVACY_URL}"
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Serveur : {message.guild.name}")
            await message.author.send(embed=embed)
        else:
            fin_timeout = datetime.now(timezone.utc) + timedelta(hours=6)
            try:
                await message.author.timeout(timedelta(hours=6), reason="3 avertissements dans le salon selfie")
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
        pass  # DMs bloqués

# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDES SLASH
# ═══════════════════════════════════════════════════════════════════════════════
def verif_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild_id or not est_admin(interaction.user, interaction.guild_id):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ─── Commandes utilisateurs : droits d'accès / suppression (ToS §5b) ─────────
@tree.command(name="mes_donnees", description="🔎 Voir les données que le bot conserve sur toi.")
async def mes_donnees(interaction: discord.Interaction):
    async with data_lock:
        data = load_data()
    uid = str(interaction.user.id)
    entrees = [users[uid] for users in data.values() if uid in users]
    if not entrees:
        texte = "✅ Le bot ne conserve **aucune donnée** à ton sujet."
    else:
        total = sum(e.get("avertissements", 0) for e in entrees)
        texte = (
            f"📦 Données conservées : ton ID Discord, l'ID du/des serveur(s), un compteur "
            f"d'avertissements et sa date de mise à jour.\n"
            f"🔢 **{total}** avertissement(s) actif(s) sur **{len(entrees)}** serveur(s).\n"
            f"🗓️ Suppression automatique après {RETENTION_JOURS} jours d'inactivité.\n"
            f"🗑️ Utilise `/supprimer_mes_donnees` pour tout effacer.\n"
            f"📄 Politique de confidentialité : {PRIVACY_URL}"
        )
    await interaction.response.send_message(texte, ephemeral=True)

@tree.command(name="supprimer_mes_donnees", description="🗑️ Supprimer toutes les données que le bot conserve sur toi.")
async def supprimer_mes_donnees(interaction: discord.Interaction):
    async with data_lock:
        data = load_data()
        retirer_utilisateur(data, interaction.user.id)
        save_data(data)
    await interaction.response.send_message("✅ Tes données ont été supprimées.", ephemeral=True)

@tree.command(name="confidentialite", description="📄 Conditions d'utilisation et politique de confidentialité.")
async def confidentialite(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"📄 Conditions : {TERMS_URL}\n🔐 Confidentialité : {PRIVACY_URL}", ephemeral=True)

# ─── Commandes admin ──────────────────────────────────────────────────────────
@tree.command(name="configurer", description="⚙️ Configurer le salon selfie et le rôle admin.")
@app_commands.guild_only()
@verif_admin()
@app_commands.describe(salon="Le salon réservé aux selfies",
                       role_admin="Le rôle considéré comme administrateur (optionnel)")
async def configurer(interaction: discord.Interaction, salon: discord.TextChannel,
                     role_admin: discord.Role = None):
    guild_config[interaction.guild_id] = {
        "channel_id": salon.id,
        "admin_role_id": role_admin.id if role_admin else None
    }
    save_guild_config()
    description = f"✅ Salon selfie configuré : {salon.mention}\n"
    description += (f"👑 Rôle admin : {role_admin.mention}" if role_admin
                    else "👑 Rôle admin : administrateurs serveur uniquement")
    await interaction.response.send_message(description, ephemeral=True)

@tree.command(name="statut", description="📊 Voir la configuration actuelle du bot.")
@app_commands.guild_only()
@verif_admin()
async def statut(interaction: discord.Interaction):
    cfg = guild_config.get(interaction.guild_id, {})
    channel_id, role_id = cfg.get("channel_id"), cfg.get("admin_role_id")
    embed = discord.Embed(title="⚙️ Configuration du bot Selfie", color=discord.Color.blurple(),
                          timestamp=datetime.now(timezone.utc))
    embed.add_field(name="📸 Salon selfie", value=f"<#{channel_id}>" if channel_id else "❌ Non configuré", inline=False)
    embed.add_field(name="👑 Rôle admin", value=f"<@&{role_id}>" if role_id else "Admins serveur uniquement", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="avertissements", description="🔍 Voir les avertissements d'un membre.")
@app_commands.guild_only()
@verif_admin()
@app_commands.describe(membre="Le membre à vérifier")
async def voir_avertissements(interaction: discord.Interaction, membre: discord.Member):
    async with data_lock:
        data = load_data()
    nb = data.get(str(interaction.guild_id), {}).get(str(membre.id), {}).get("avertissements", 0)
    embed = discord.Embed(title=f"⚠️ Avertissements de {membre.display_name}",
                          description=f"🔢 **{nb}/{MAX_AVERT}** avertissement(s)",
                          color=discord.Color.orange() if nb > 0 else discord.Color.green(),
                          timestamp=datetime.now(timezone.utc))
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="reinitialiser", description="🔄 Remettre à zéro les avertissements d'un membre.")
@app_commands.guild_only()
@verif_admin()
@app_commands.describe(membre="Le membre à réinitialiser")
async def reinitialiser(interaction: discord.Interaction, membre: discord.Member):
    async with data_lock:
        data = load_data()
        retirer_utilisateur(data, membre.id, interaction.guild_id)
        save_data(data)
    await interaction.response.send_message(
        f"✅ Avertissements de **{membre.display_name}** remis à zéro.", ephemeral=True)

@tree.command(name="forcer_avertissement", description="⚠️ Donner manuellement un avertissement à un membre.")
@app_commands.guild_only()
@verif_admin()
@app_commands.describe(membre="Le membre à avertir", raison="Raison de l'avertissement (non conservée)")
async def forcer_avertissement(interaction: discord.Interaction, membre: discord.Member,
                               raison: str = "Non spécifiée"):
    nb = await ajouter_avertissement(interaction.guild_id, membre.id)
    if nb >= MAX_AVERT:
        try:
            await membre.timeout(timedelta(hours=6), reason=raison)
        except discord.Forbidden:
            pass
        await interaction.response.send_message(
            f"🔇 **{membre.display_name}** a atteint 3 avertissements → mis en sourdine 6h. Raison : {raison}",
            ephemeral=True)
        embed = discord.Embed(title="🔇 Mise en sourdine",
                              description=f"Tu as été mis en sourdine 6h sur **{interaction.guild.name}**.\nRaison : {raison}",
                              color=discord.Color.red())
    else:
        await interaction.response.send_message(
            f"⚠️ Avertissement **{nb}/{MAX_AVERT}** ajouté à **{membre.display_name}**. Raison : {raison}",
            ephemeral=True)
        embed = discord.Embed(title="⚠️ Avertissement",
                              description=f"Tu as reçu un avertissement sur **{interaction.guild.name}**.\nRaison : {raison}\nAvertissements : **{nb}/{MAX_AVERT}**",
                              color=discord.Color.orange())
    try:
        await membre.send(embed=embed)
    except discord.Forbidden:
        pass

# ─── Serveur HTTP keep-alive ──────────────────────────────────────────────────
async def handle_ping(request):
    return web.Response(text="✅ SelfieBot opérationnel", status=200)

async def demarrer_serveur_http():
    app_http = web.Application()
    app_http.router.add_get("/", handle_ping)
    app_http.router.add_get("/ping", handle_ping)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app_http)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Serveur HTTP keep-alive démarré sur le port {port}")

# ─── Lancement ────────────────────────────────────────────────────────────────
async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("❌ Variable d'environnement DISCORD_TOKEN manquante.")
    init_crypto()
    migrer_ancien_stockage()
    load_guild_config()
    await demarrer_serveur_http()
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
