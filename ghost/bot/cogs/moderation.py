"""Moderação automática: anti-spam, limite de mensagens em threads, cargo de entrada e anti-nuke."""

import logging
from collections import defaultdict

import discord
from discord.ext import commands

from ...config import settings
from ...services import antinuke, backup, equipe, notify, papeis, raid, incidente
from ...config.settings import CORES
from ..router import route
from ...data import store
from ...ui.emojis import E

log = logging.getLogger("moderation")


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._mensagens_thread: dict[int, int] = defaultdict(int)

    async def screen(self, message: discord.Message) -> bool:
        """Retorna True quando a mensagem foi barrada e não deve gerar comando."""
        if message.guild is not None:
            raid.observar_comando_suspeito(message)
        if await incidente.filtrar(message):
            return True
        if message.guild is None or not (raid.ligado() or raid.spam_ligado()):
            return False
        # Aplicativos externos podem publicar como bot/webhook, mas a interação
        # traz o membro que executou o comando. Preserve essa autoria para que o
        # anti-raid puna o executor junto do aplicativo.
        if raid.mensagem_aplicativo_externo(message):
            return await raid.mensagem_bot(message)
        if message.webhook_id is not None:
            return await raid.mensagem_webhook(message)
        if message.author.bot:
            return await raid.mensagem_bot(message)
        if (
            raid.protegido(message.author.id)
            or message.guild.get_member(message.author.id) is None
        ):
            return False
        if await raid.mensagem_humana(message):
            return True

        if isinstance(message.channel, discord.Thread) and raid.spam_ligado():
            tid = message.channel.id
            self._mensagens_thread[tid] += 1
            if self._mensagens_thread[tid] >= settings.MAX_THREAD_MSGS:
                if store.thread_owner(tid) is not None:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                    return True
                try:
                    await message.channel.send(
                        f"{E.alert} Limite de mensagens atingido. Esta thread será fechada."
                    )
                    await message.channel.delete()
                except discord.HTTPException:
                    pass
                return True
        return False

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        if entry.guild.id == settings.GUILD_ID:
            await raid.auditoria_extra(entry)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id != settings.GUILD_ID or raid.protegido(payload.user_id):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        abusivo = raid.estourou("reaction", payload.user_id)
        if abusivo:
            await raid.punir(guild, payload.user_id, "Spam de reações")
        if abusivo or incidente.ativo(guild.id):
            try:
                channel = self.bot.get_channel(
                    payload.channel_id
                ) or await self.bot.fetch_channel(payload.channel_id)
                await channel.get_partial_message(payload.message_id).remove_reaction(
                    payload.emoji, discord.Object(id=payload.user_id)
                )
            except discord.HTTPException:
                log.warning("Não foi possível remover reação durante contenção")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if payload.guild_id != settings.GUILD_ID:
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            await self.screen(message)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        # Não atualiza contadores aqui: convite de uso único pode sumir antes do MEMBER_JOIN.
        # Esse caso ficará indeterminado, em vez de atribuir o usuário ao convite errado.
        pass

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        if getattr(getattr(thread, "guild", None), "id", None) != settings.GUILD_ID:
            return
        self._mensagens_thread.pop(thread.id, None)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if getattr(getattr(thread, "guild", None), "id", None) != settings.GUILD_ID:
            return
        # Gateway também emite THREAD_CREATE quando ganhamos acesso a tópico existente.
        if (discord.utils.utcnow() - thread.created_at).total_seconds() < 30:
            await raid.thread_criada(thread)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        if getattr(getattr(channel, "guild", None), "id", None) != settings.GUILD_ID:
            return
        await raid.webhooks_criados(channel.guild)

    @commands.Cog.listener()
    async def on_guild_update(
        self, before: discord.Guild, after: discord.Guild
    ) -> None:
        if before.id != settings.GUILD_ID:
            return
        if (
            incidente.ativo(after.id)
            and incidente.estado(after.id).get("fase") != "liberacao"
            and not after.invites_paused()
        ):
            incidente.estado(after.id)["pausa_ok"] = False
            await incidente.pausar(after)
        """Nome, descrição ou ícone trocados durante uma raid voltam ao que estava no backup."""
        if backup.em_raid() and (before.name, before.description, before.icon) != (
            after.name,
            after.description,
            after.icon,
        ):
            backup.agendar_restauracao(after)

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        if getattr(getattr(before, "guild", None), "id", None) != settings.GUILD_ID:
            return
        await raid.canal_editado(after)
        if not backup.em_raid():
            antinuke._save(after)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        if getattr(getattr(role, "guild", None), "id", None) != settings.GUILD_ID:
            return
        await raid.cargo_criado(role)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if getattr(getattr(role, "guild", None), "id", None) != settings.GUILD_ID:
            return
        await raid.cargo_removido(role)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after) -> None:
        if guild.id != settings.GUILD_ID:
            return
        if len(after) > len(before):
            await raid.emojis_criados(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if getattr(getattr(invite, "guild", None), "id", None) != settings.GUILD_ID:
            return
        await raid.convite_criado(invite)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User) -> None:
        if guild.id != settings.GUILD_ID:
            return
        await raid.banido(guild, user)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if getattr(getattr(member, "guild", None), "id", None) != settings.GUILD_ID:
            return
        await raid.expulso(member)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if after.guild.id != settings.GUILD_ID or not incidente.alvo(
            after.guild.id, after.id
        ):
            return
        pena = incidente.estado(after.guild.id)["alvos"][str(after.id)]
        if (
            any(not r.is_default() and not r.managed for r in after.roles)
            or not after.is_timed_out()
        ):
            await incidente.sancionar(
                after.guild,
                after.id,
                pena["motivo"],
                banir=pena["pena"] == "ban",
                reaplicar=True,
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if getattr(getattr(member, "guild", None), "id", None) != settings.GUILD_ID:
            return
        bloqueado = await incidente.entrada(member)
        await raid.bot_entrou(member)
        if (
            bloqueado
            or incidente.ativo(member.guild.id)
            or incidente.alvo(member.guild.id, member.id)
        ):
            return
        if member.bot:
            await raid.pedir_confirmacao_bot(member)
            return
        if store.is_admin(member.id):
            await equipe.aplicar(member.guild, member.id, True)
        cargo = papeis.fixo(member.guild, "membro")
        if cargo:
            try:
                await member.add_roles(cargo)
            except discord.HTTPException:
                pass

    @route(prefix="bot:")
    async def _confirmar_bot(self, interaction: discord.Interaction, cid: str) -> None:
        """Botões do pedido de confirmação de bot novo. Só o dono decide."""
        if interaction.user.id != settings.ADMIN_ID:
            await interaction.response.send_message(
                f"{E.wrong} Só o dono confirma bots.", ephemeral=True
            )
            return
        partes = cid.split(":")
        if len(partes) != 3 or not partes[2].isdigit() or partes[1] not in ("ok", "sem", "no"):
            await interaction.response.send_message(
                f"{E.wrong} Ação desconhecida.", ephemeral=True
            )
            return
        guild = interaction.guild
        membro = guild.get_member(int(partes[2])) if guild else None
        if membro is None:
            texto = f"{E.wrong} O bot não está mais no servidor."
        elif partes[1] == "ok":
            cargo = papeis.fixo(guild, "bots")
            if cargo is None:
                texto = (
                    f"{E.wrong} Cargo {settings.CARGO_BOTS} não encontrado. Crie o cargo com esse nome."
                )
            else:
                await membro.add_roles(cargo, reason="Confirmado pelo dono")
                texto = f"{E.correct} Confirmado por {interaction.user}: bot recebeu o cargo {settings.CARGO_BOTS}."
        elif partes[1] == "sem":
            texto = f"{E.correct} Confirmado por {interaction.user}: bot entrou sem cargo."
        else:
            try:
                await membro.kick(reason="Recusado pelo dono")
                texto = f"{E.wrong} Recusado por {interaction.user}: bot expulso."
            except discord.HTTPException as exc:
                texto = f"{E.alert} Não consegui expulsar o bot: {exc}"
        await interaction.response.edit_message(content=texto, view=None)
        await notify.registrar(
            "config_alterada",
            f"{E.shield} Bot confirmado ou recusado",
            CORES["info"],
            [
                ("Bot", partes[2]),
                ("Decisão", partes[1]),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if getattr(getattr(channel, "guild", None), "id", None) != settings.GUILD_ID:
            return
        await antinuke.on_channel_created(channel)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if getattr(getattr(channel, "guild", None), "id", None) != settings.GUILD_ID:
            return
        await antinuke.on_channel_deleted(channel)

    @commands.command(name="raidstatus")
    async def raidstatus(self, ctx):
        if ctx.author.id != settings.ADMIN_ID:
            return
        s = incidente.estado(settings.GUILD_ID)
        await ctx.send(
            f"Proteção: {s.get('ativo', False)} | Fase: {s.get('fase', 'nenhuma')}\n"
            f"Backup: {s.get('backup') or 'ausente'}\n"
            f"Pendências: {str(s.get('erro') or s.get('falhas') or 'nenhuma')[:1400]}"
        )

    @commands.command(name="raidliberar")
    async def raidliberar(self, ctx, user_id: int):
        if ctx.author.id != settings.ADMIN_ID:
            return
        guild = self.bot.get_guild(settings.GUILD_ID)
        if guild is None or incidente.ativo(guild.id):
            await ctx.send("Finalize a contenção antes de revisar uma pena.")
            return
        s = incidente.estado(guild.id)
        pena = s.get("alvos", {}).pop(str(user_id), None)
        if pena is None:
            await ctx.send("Sem sanção registrada para esse membro.")
            return
        incidente.salvar(guild.id)
        try:
            if pena["pena"] == "ban":
                await guild.unban(
                    discord.Object(id=user_id), reason="Revisão da pena pelo dono"
                )
            else:
                membro = guild.get_member(user_id) or await guild.fetch_member(user_id)
                await membro.timeout(None, reason="Revisão da pena pelo dono")
                for cid, valores in pena.get("permissoes_antes", {}).items():
                    channel = guild.get_channel(int(cid))
                    if channel:
                        overwrite = discord.PermissionOverwrite.from_pair(
                            discord.Permissions(valores[0]),
                            discord.Permissions(valores[1]),
                        )
                        await channel.set_permissions(
                            membro,
                            overwrite=None if overwrite.is_empty() else overwrite,
                            reason="Revisão da pena pelo dono",
                        )
            await ctx.send(
                "Pena removida. Cargos antigos não são devolvidos automaticamente; revise-os antes de conceder acesso."
            )
        except discord.HTTPException as exc:
            s.setdefault("alvos", {})[str(user_id)] = pena
            incidente.salvar(guild.id)
            await ctx.send(
                f"Revisão incompleta: {type(exc).__name__}. A sanção continua registrada."
            )

    @commands.command(name="raidretomar")
    async def raidretomar(self, ctx):
        if ctx.author.id != settings.ADMIN_ID:
            return
        guild = self.bot.get_guild(settings.GUILD_ID)
        if guild and incidente.ativo(guild.id):
            await incidente.iniciar(
                guild, "Nova tentativa solicitada pelo dono", tentar_novamente=True
            )
            await ctx.send(
                "Contenção retomada. A liberação continua condicionada à restauração sem falhas."
            )

    @commands.command(name="reverter")
    @commands.dm_only()
    async def reverter(self, ctx: commands.Context) -> None:
        if ctx.author.id != settings.ADMIN_ID:
            return
        if incidente.ativo(settings.GUILD_ID):
            await ctx.send(
                "Incidente ativo: a liberação depende da neutralização e restauração sem falhas. Consulte o diário."
            )
            return
        if not antinuke.lockdown_active():
            await ctx.send(f"{E.information} Nenhum lockdown ativo.")
            return
        await ctx.send(f"{E.reload} Revertendo lockdown...")
        for guild in self.bot.guilds:
            await antinuke.revert(guild)
        await ctx.send(f"{E.correct} Lockdown revertido.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
