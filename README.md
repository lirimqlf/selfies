# 📸 SelfieBot

Bot Discord de modération d'un salon « selfies » : une photo par message, avertissements (3 max), sourdine 6 h, fil de discussion sous chaque selfie valide.

## Installation
1. `pip install -r requirements.txt`
2. Copier `env.example` en `.env` et remplir `DISCORD_TOKEN` + `DATA_ENCRYPTION_KEY` :
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   ⚠️ Sauvegardez cette clé hors du dépôt : sans elle, les données chiffrées sont irrécupérables.
3. `python bot.py` (ou Docker : `docker build -t selfies . && docker run --env-file .env -v selfies-data:/data selfies`)

## Conformité aux Discord Developer Terms
| Exigence | Mise en œuvre |
|---|---|
| Chiffrement des données au repos (§5c) | `data.enc` / `guild_config.enc` chiffrés (Fernet), fichiers en `0600`, migration auto des anciens `.json` |
| Identifiants non exposés (§2d) | Token et clé via variables d'environnement / GitHub Secrets, `.env` ignoré par git |
| Politique de confidentialité & CGU (§3b, §5a) | `docs/privacy.html`, `docs/terms.html` (à publier via GitHub Pages) |
| Suppression / modification des données (§5b) | `/mes_donnees`, `/supprimer_mes_donnees`, `/reinitialiser`, purge après 90 j, suppression au retrait du bot |
| Minimisation | Aucun intent privilégié, contenu des messages non lu, motif non stocké |
| Pas de suggestion de partenariat (§8c) | Mention de non-affiliation dans les pages |

## À faire de votre côté
- Remplacer `[EMAIL DE CONTACT]`, `[NOM / PSEUDO DU RESPONSABLE]`, `[HÉBERGEUR]` dans `docs/*.html` et `VOTRE-PSEUDO` dans `env.example`.
- Activer GitHub Pages (Settings → Pages → branche `main`, dossier `/docs`).
- Dans le [Developer Portal](https://discord.com/developers/applications) → General Information : renseigner une description fidèle, l'URL des Terms of Service et de la Privacy Policy.
- Ajouter le secret `DATA_ENCRYPTION_KEY` (et les variables `PRIVACY_URL` / `TERMS_URL`) dans les paramètres GitHub Actions.

## Incident de sécurité
En cas de fuite du token ou des données : régénérer le token (Developer Portal), changer la clé de chiffrement, prévenir Discord et les utilisateurs concernés sans délai (§5c).
