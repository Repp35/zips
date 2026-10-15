# Ghost Bot

Bot de loja para Discord com comandos de barra e prefixo, catálogo JSON, vitrines, carrinhos privados, pagamento PIX, entrega automática/manual, suporte, proteções e agente com aprovação do dono.

## Instalação

Requer Python 3.10 ou posterior.

1. Instale as dependências:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Preencha o `.env` com o token do bot e os IDs necessários. Para uma instalação nova, use `.env.example` como modelo.
3. Inicie:

   ```bash
   python main.py
   ```

Ative os intents de membros e conteúdo de mensagens no portal do Discord. O cargo do bot precisa estar acima dos cargos que ele administra. Uma instalação deve usar apenas um processo por banco SQLite.

Os logs do processo são enviados para `stderr` e incluem nível, módulo, arquivo e
linha. Exceções registradas com `logger.exception` ou `exc_info` mostram o traceback
completo. Falhas não tratadas em tasks assíncronas também são encaminhadas ao logger
principal, e avisos do Python são capturados.

Os caminhos são calculados a partir da pasta do projeto, e não do diretório atual do terminal. O `.env` fica na raiz; o banco SQLite e o diário ficam em `data/` (`data/banco.db` e `data/diario.jsonl`). Para usar outro local, defina `DB_PATH` ou `DIARIO_PATH`; caminhos relativos continuam partindo da raiz do projeto.

## Banco de dados

O banco é inicializado automaticamente no primeiro start. `ghost/database/schema.py` define as tabelas e os índices atuais; `ghost/database/migrations.py` aplica mudanças versionadas numa transação; `ghost/database/connection.py` concentra conexão, serialização, commit/rollback e savepoints. Os módulos de negócio usam essa camada e não criam tabelas sob demanda. Um arquivo vazio recebe o schema atual sem depender dos IDs ou da estrutura de dados do servidor Discord. `schema_migrations` registra a versão aplicada e impede que uma versão mais antiga do bot abra silenciosamente um banco mais novo.

O SQLite usa WAL, chaves estrangeiras, `synchronous=FULL`, espera limitada por bloqueio e permissões locais restritas. As operações do processo usam uma conexão serializada; rode apenas uma instância do bot por arquivo SQLite. SQL bruto e valores de parâmetros não são copiados para o diário, evitando registrar senhas e dados privados. Eventos de negócio continuam auditáveis.

Apagar `data/banco.db` inicia um banco vazio e perde dados locais como estoque importado, pedidos, cupons, reservas, estados de suporte e registros de segurança. Se for apagar, pare o bot primeiro e remova também `data/banco.db-wal` e `data/banco.db-shm`, se existirem. O `.env`, catálogo e backups de servidor são arquivos separados e não são apagados pelo inicializador.

## Dados incluídos

Este ZIP mantém o `.env`, o diário, os backups existentes, o catálogo e os assets. Ele não inclui o banco SQLite, os arquivos WAL/SHM, testes ou caches; o banco vazio é criado no primeiro start conforme a seção acima. Não compartilhe o ZIP: o `.env` e o diário podem conter credenciais e dados privados.

## Ghost Agent

Depois de iniciar uma sessão com `/agente controle` (`iniciar sessão neste canal`) ou definir o canal fixo, mensagens normais do dono são avaliadas pelo próprio provedor para entender se foram dirigidas ao agente; não é necessário mencionar o bot nem usar uma palavra-chave. O padrão é responder, inclusive a saudações como “Iai”; o agente só fica em silêncio quando há evidência clara de que a fala é para outra pessoa. A classificação usa uma chamada sem ferramentas antes da chamada de ação, para impedir que uma conversa casual acione mudanças no servidor.

## Backup do servidor

Backups novos guardam os campos de configuração que a API expõe, permissões, cargos administráveis, canais, tópicos, emojis, figurinhas, webhooks, convites, bans, dados de perfil dos membros presentes e mensagens acessíveis em canais e tópicos. O pareamento de canais e cargos usa tipo, propriedades, nome e estrutura; os IDs antigos servem para remapear referências, não para decidir equivalência. Mensagens são reconciliadas por autor e conteúdo, com anexos/embeds quando disponíveis. Mensagens faltantes são enviadas na ordem do backup, mas mensagens que já existem não podem ser reposicionadas: se houver lacunas no meio do histórico, o Discord não permite inserir mensagens retroativamente ali. A réplica também recebe novos IDs e horários de envio. Reenvios confirmados ficam registrados para evitar duplicações em retomadas.

O bot cancela a criação se não conseguir ler os históricos e membros necessários. Durante o backup completo, anexos são baixados para `backups/anexos/`; o arquivamento incremental também baixa os anexos antes de avançar o cursor e deixa a mensagem pendente para nova tentativa se algum download falhar. Snapshots estruturais válidos com histórico incompleto podem restaurar canais, cargos e configurações; a restauração informa os itens faltantes, não apaga mensagens atuais sem um histórico completo, e permite reproduzir apenas as mensagens que foram salvas, após autorização explícita. Uma réplica visual não é o registro nativo original: mensagens republicadas recebem novos IDs e são enviadas pelo webhook; mídias só são recuperáveis se os bytes foram preservados no armazenamento local ou em uma cópia externa. Itens gerenciados por integrações, membros que já saíram, recursos que a API não expõe e certas configurações exclusivas do dono não podem ser copiados integralmente. Acesso, hierarquia e limites da API também podem impedir alterações.

### Cópia no Google Drive

O snapshot baixa os anexos do Discord para `backups/anexos/` antes de concluir o backup. Com `GOOGLE_DRIVE_ENABLED=true`, o bot sincroniza a pasta `backups/` com a conta Google autorizada depois de criar o snapshot e também tenta retomar a fila ao iniciar. São enviados os manifestos, históricos incrementais, fotos e arquivos de mídia. O índice em `data/google-drive-state.json` permite ignorar arquivos sem alteração e continuar depois de uma queda; uploads grandes usam a sessão retomável da Drive API. Falhas no Drive aparecem no log e mantêm intacta a cópia local.

Para configurar:

1. No Google Cloud, habilite Google Drive API e crie um cliente OAuth do tipo **Aplicativo para computador**. Baixe o JSON OAuth e substitua o modelo `google-drive-credentials.json` da raiz pelo arquivo baixado (ou copie para ele mantendo a estrutura `installed`). Não cole o JSON em canais públicos nem no repositório.
2. Gere a autorização uma vez com `python -m ghost.services.google_drive_auth`. O token é salvo no caminho `GOOGLE_DRIVE_TOKEN_FILE` (`data/google-drive-token.json` por padrão). Esse arquivo é secreto e não deve ser enviado junto com o projeto.
3. `GOOGLE_DRIVE_FOLDER_ID` vazio cria/usa a pasta `Ghost Bot Backups`; se preenchido, o bot usa a pasta indicada. A integração solicita escopo de acesso ao Drive da conta para poder escrever nessa pasta; use uma conta Google dedicada aos backups se quiser limitar o impacto desse acesso.

Como o bot pode rodar no Android/Pydroid, faça a autorização inicial em um computador desktop se o callback local não funcionar: o Google marca o fluxo de loopback como obsoleto para clientes OAuth móveis. Copie o token gerado para o caminho configurado no servidor. O refresh token mantém a autorização entre reinicializações; revogue o acesso nas configurações da conta Google se perder o token.

No Google Auth Platform, adicionar a conta como usuário de teste deixa o consentimento em modo **Testing**; nessa condição, autorizações para escopos do Drive e os refresh tokens expiram após sete dias. Para backups automáticos contínuos, publique o app como **In production** (ou use o modo interno de um Google Workspace); o Google pode exigir verificação para os escopos solicitados. Consulte a configuração atual de audiência e validade no [guia oficial de audiência OAuth](https://support.google.com/cloud/answer/15549945?hl=pt-br).

`GOOGLE_DRIVE_COMPRESS=true` aplica gzip sem perda a arquivos que economizem pelo menos 5%; formatos que já costumam ser comprimidos, como JPEG, PNG, GIF, MP4, WebP e ZIP, são enviados sem recompressão. Portanto, reduzir todo arquivo à metade não é garantido: mídia já comprimida quase nunca permite essa redução sem perda de qualidade. O Drive guarda o original lógico por metadados e o restore baixa e descomprime a mídia que faltar localmente.

Antes de restaurar, crie um backup atual e teste em um servidor separado. Para restaurar, o bot precisa das permissões de gerenciar servidor, canais, cargos, emojis/figurinhas e membros, conforme as funções usadas.


## Anti-raid desta versão

A proteção usa um incidente persistente no SQLite. A sequência é: registrar o incidente e fixar o backup anterior, neutralizar os responsáveis, pausar convites, conter novas alterações, esperar **20 segundos sem atividade**, restaurar e só então liberar. O intervalo reinicia com novas ações. Falha de hierarquia, punição, leitura ou restauração mantém a proteção; não há liberação automática por um timer cego. Reiniciar o processo retoma a contenção. Uma confirmação para reproduzir o histórico comum não bloqueia a inspeção, a contenção, a restauração estrutural nem as demais funções do bot. Banimentos automáticos do Anti-raid pedem ao Discord para apagar as mensagens do responsável nos **sete dias anteriores**, o máximo aceito pelo endpoint; mensagens mais antigas não são removidas por essa opção.

- Mensagens novas e editadas de todos, inclusive deste bot e de outros bots/webhooks, são apagadas durante a contenção; só o dono real do servidor fica livre. Na fase de restauração, apenas os webhooks pertencentes a este bot e registrados pelo restaurador podem repor o histórico. Comandos de outros membros ficam suspensos.
- Novos canais são apagados; exclusões de canais do backup disparam reposição, depois da tentativa de neutralizar o autor. Ao concluir a fase estrutural, o mapeamento antigo→novo atualiza referências no banco, catálogo, `.env` (todas as variáveis de canal definidas) e mensagens de setup; a fase de vitrines confere e republica apenas o que estiver faltando. Tópicos, webhooks, cargos, permissões, bans/expulsões, convites, emojis, figurinhas, integrações, regras de AutoMod, eventos agendados, soundboard, reações e entradas em massa têm detecção. Eventos de auditoria do próprio restaurador não viram ataques contra ele mesmo.
- A detecção de ações estruturais usa os mesmos limites por autor para membros e bots; mensagens automatizadas por selfbot são analisadas como mensagens da própria conta. Um comando reconhecido só abre uma janela curta de atenção; a palavra sozinha não pune. Ações destrutivas correlacionadas e reincidência podem levar à tentativa de banimento. O usuário identificado como adicionador de um bot malicioso recebe contenção separada; o fato de convidar, por si só, não é prova de que executou a raid. Spam humano usa a escada configurada de sanções. Hierarquia insuficiente é registrada como falha, nunca como punição aplicada. Só o dono real do servidor é excluído das sanções automáticas.
- Canais: a janela padrão é **5 segundos**; o terceiro canal criado pelo mesmo autor ou a segunda exclusão pelo mesmo autor ultrapassa o limite e inicia a contenção. Três canais do mesmo tipo com nome repetido dentro da janela também iniciam a contenção. O painel Anti-raid permite ajustar separadamente o máximo e a janela de cada ação; a próxima ação após o máximo confirma o excesso. A contagem é por autor para não somar operações legítimas de pessoas diferentes.
- Após reconectar, o bot faz uma inspeção estrutural local a cada `raid.inspecao_intervalo_s` (padrão: 30 s), usando o cache do gateway e o snapshot em cache, sem consultar históricos repetidamente. A varredura profunda de mensagens roda em segundo plano na inicialização; um excesso estrutural claro inicia contenção antes dessa varredura. Ela prioriza até `raid.scan_canais_offline_max` canais (padrão: 60) quando há centenas de extras.
- A atribuição usa `CHANNEL_CREATE`, `CHANNEL_DELETE`, `THREAD_CREATE` e eventos de auditoria. A documentação oficial confirma esses eventos e que `GUILD_AUDIT_LOG_ENTRY_CREATE` só chega a bots com `VIEW_AUDIT_LOG`: [Gateway Events do Discord](https://discord.com/developers/docs/events/gateway-events).
- A relação bot → quem o adicionou usa o alvo exato da auditoria. A relação membro → criador de convite é inferida pela diferença de usos observada na entrada. Entradas simultâneas, convites de uso único desaparecidos, vanity links e falta de acesso podem deixá-la indeterminada. O criador não necessariamente foi quem compartilhou o link. O registro deixa isso explícito.
- Revoga convites dos responsáveis e do criador ligado à entrada. Bane membros associados sem ambiguidade de contador a esses convites e que entraram desde as **3 horas anteriores** ao início do incidente; não expande uma cadeia ilimitada. Quem convidou alguém não é punido apenas por essa associação, mas os convites relacionados são revogados. O histórico começa quando esta versão passa a observar entradas; não é possível reconstruir retroativamente quem usou cada convite antes disso.
- As sanções sobrevivem à restauração: cargos do snapshot não voltam aos alvos. Logs ficam no diário durante o bloqueio; a conclusão e as cadeias de entrada são enviadas ao canal de logs após a liberação.

Comandos do dono configurado, também utilizáveis por DM: `>raidstatus` mostra fase e pendências; `>raidretomar` tenta novamente após corrigir a causa da falha; `>raidliberar ID` revisa uma sanção depois do incidente e não concede cargos antigos automaticamente. O prefixo segue a configuração do bot. `>reverter` não contorna uma contenção ativa.

A pausa tenta primeiro o recurso persistente do Discord e, se indisponível, usa a pausa temporária de 24h renovada durante a execução. O bot precisa permanecer online; perder o token, ser expulso, perder permissões ou ficar abaixo do agressor na hierarquia impede a atuação. A API aplica limites de requisições: apagar mensagens e reconstruir canais não é instantâneo.

Na inicialização, com o Anti-raid ligado, a inspeção estática procura diferenças estruturais em relação ao backup, canais extras/repetidos, cargos e permissões alterados, tópicos duplicados e conteúdo repetido recente. Depois, a checagem estrutural leve roda a cada `raid.inspecao_intervalo_s`; a leitura de mensagens roda em segundo plano e amostra até `raid.scan_canais_offline_max` canais. A inspeção não roda com o Anti-raid desligado. Respostas de aplicativos externos são atribuídas ao usuário por `interaction_metadata.user` quando esse metadado existe; em mensagens antigas ou sem essa informação, o bot não presume quem foi o autor. O Discord pode omitir o conteúdo de mensagens sem acesso à intenção privilegiada `MESSAGE_CONTENT`. A inspeção não percorre todo o histórico de mensagens.

Uma raid ocorrida offline pode ser reconhecida por indícios que ainda existam no servidor e pelo snapshot. Mensagens que já foram apagadas e não estão no backup não podem ser recuperadas nem analisadas retroativamente; a atribuição depende dos metadados disponíveis e do registro de auditoria do Discord. O Discord mantém entradas de auditoria por 45 dias, mas o bot usa apenas entradas recentes e correlacionáveis para reduzir punições por coincidência. Portanto, nenhum sistema pode prometer detectar e atribuir toda raid em qualquer cenário offline.

Aplicativos externos recebem tentativa de banimento quando o spam fica ligado a uma interação suspeita. Se o aplicativo não for membro do servidor e a API não permitir o banimento, o bot bloqueia o uso de apps externos no canal afetado, remove as mensagens acessíveis e continua a contenção/restauração. Selfbot não é identificável como categoria separada: a conta continua sendo avaliada como membro, por mensagens e ações atribuídas a ela.

Comandos claramente destrutivos no início de uma mensagem, como `.nuke`, `>nuke`, `!nuke`, `/nuke`, `>banall`, `.massban`, `.wipe`, `.destroy`, `.spam`, `.threadspam` e `.webhookspam`, abrem uma janela curta de atenção para acelerar a resposta. Prefixos comuns e o prefixo configurado são aceitos; a palavra no meio de uma frase não ativa a janela. O comando sozinho nunca pune ninguém. A proteção comportamental roda independentemente dessa janela: ela observa rajadas e repetição de mensagens, ações estruturais pela auditoria e padrões de criação/exclusão em massa, inclusive de membros que automatizam a conta (selfbot). Ação estrutural correlacionada ao próprio usuário ou spam já confirmado nessa janela eleva a resposta para contenção imediata. Um app que responde com linguagem de ataque pode ser associado ao usuário de `interaction_metadata.user` e observado por três minutos. Para slash commands de outros apps, o Discord não entrega ao bot o nome e as opções do comando como se fossem comandos do próprio bot, então a correlação usa a resposta pública e os metadados disponíveis. Não se pune apenas por palavra-chave ou por uma mensagem isolada.

**Há limites que a API do Discord impõe à réplica.** O bot salva e reconcilia os campos estruturais disponíveis, incluindo configuração do servidor, canais e categorias, cargos e permissões, threads, membros presentes, bans, convites, webhooks, figurinhas, AutoMod e eventos agendados. Convites recriados recebem códigos novos; webhooks recriados recebem IDs/tokens novos; estados de RSVP e objetos geridos por integrações não podem ser reproduzidos como os originais. Mensagens reconstruídas preservam visualmente autor, avatar disponível e horário, mas recebem IDs novos; anexos dependem de URLs ainda válidas. Permissões, hierarquia e limites da API podem impedir ações. Faça o teste em um servidor separado antes de depender da recuperação automática.

Validação desta revisão: `compileall` e análise sintática dos módulos alterados passaram. A revisão teórica verificou retomada por checkpoint, backup sem mensagens completas, prompt com e sem mensagens salvas, falha em vitrine, falha por etapa, confirmação pendente após reinício e estado terminal com avisos. Não foi possível importar ou executar testes locais neste ambiente porque `discord.py` e `python-dotenv` não estão instalados. Nenhuma ação foi executada no Discord.

## Backup e recuperação (revisão)

O backup automático mantém um único arquivo, `backups/backup-diario.json`, que é
atualizado no lugar a cada ciclo diário e aparece no topo da lista. O manifesto
contém referências incrementais aos objetos de conteúdo; objetos sem mudança são
reutilizados. Os backups manuais continuam criando arquivos separados e têm sua
própria retenção. Os objetos em `backups/objetos/` são compartilhados entre versões;
**copie essa pasta junto com os manifestos**. Backups antigos em JSON completo
continuam legíveis e os antigos diários são removidos após a migração bem-sucedida
para o arquivo fixo. Um backup com histórico incompleto é descartado. Durante raid
ou restore, novos backups são adiados. Não remova o banco e os backups ao atualizar
o bot.

O vencimento diário é calculado com o horário persistido no manifesto, então reiniciar
ou deixar o bot offline não zera o período. Na reconexão, o loop executa uma passada
imediata: atualiza o backup vencido, arquiva mensagens ainda acessíveis desde o
último cursor e verifica tarefas com prazo vencido. Ele consolida ciclos perdidos em
uma execução atual, sem disparar em rajada uma execução para cada hora offline.
Estados que não foram registrados enquanto o bot estava desligado não podem ser
reconstruídos retroativamente; mensagens apagadas nesse intervalo também não podem
ser buscadas pela API.

O arquivamento periódico usa o cursor de cada canal e busca apenas mensagens novas.
O restore lê as páginas completas em ordem e detecta páginas que não avançam.
Uma interrupção inesperada mantém um checkpoint retomável. Falhas recuperáveis
são registradas e as outras etapas continuam; ao final, `concluido_com_avisos`
lista os itens que faltaram. Posições de canais são aplicadas em lote; chamadas REST usam
os headers de bucket/global e aguardam o `retry_after` recebido. Os limites do
Discord podem variar ou ser compartilhados com outros processos usando o mesmo
token, então não há como garantir que a API nunca responda `429`; se acontecer,
o cliente aguarda o tempo informado antes de repetir a chamada.

O restore compara canais e cargos por tipo, estrutura e propriedades, usando IDs
antigos apenas como chaves para atualizar referências depois. A ordem é: remover
canais excedentes com casamento por multiplicidade, posição e contexto; limpar
mensagens que não pertencem ao snapshot nos canais mantidos, incluindo mensagens
do próprio bot que não correspondam ao backup (controles de confirmação/vitrine
são preservados); então reconstruir canais, cargos, permissões e os demais
recursos. Canais iguais são casados um a um pela posição mais próxima; itens sem
par são removidos e recriados quando necessário, sem deixar extras por uma
ambiguidade do matcher. Uma listagem incompleta de tópicos arquivados ou uma
falha de API fica no relatório final de avisos; não reinicia o restore em loop.
Use `/restauracao parar`, `/restauracao continuar` e `/restauracao estado` para controlar o restore. Um novo `/backup restaurar` cancela a execução anterior, aguarda a tarefa encerrar e começa a reconciliação do backup escolhido. Cada fase recuperável continua apesar de falhas parciais; o estado terminal `concluido_com_avisos` lista os itens pendentes sem reiniciar automaticamente a mesma tentativa.

Vitrines são reconhecidas por seus botões e verificadas automaticamente. Mensagens
comuns, inclusive mensagens do bot fora das vitrines, exigem autorização explícita
para replay. Mensagens de controle do restore são preservadas. O Discord atribui
novos IDs e horários às mensagens reenviadas; convites recriados também recebem
novos códigos. Reações, membros que deixaram o servidor e recursos que a API não
expõe não podem ser reconstruídos fielmente. Faça uma cópia de `backups/`, `data/`
e `.env` antes de substituir uma instalação que já está em uso.

Ao terminar, o canal de logs recebe **Restauração concluída** ou
**Restauração concluída com avisos**. Uma interrupção inesperada fica marcada
como incompleta e pode ser retomada.

Os limites ficam separados em `config/config.json`: `raid` controla detecção e
inspeção offline; `spam` controla rajadas, sequência e repetição entre canais;
`penalidades` controla janela de reincidência e duração dos timeouts; `backup`
controla retenção, frequência e espaçamento das operações; `incidente` controla
tempo de silêncio, janela de atribuição e concorrência de exclusões. Valores
operacionais novos são limitados a faixas seguras ao iniciar. Limites de ações
de raid também podem ser ajustados no painel Anti-raid e ficam no banco.
