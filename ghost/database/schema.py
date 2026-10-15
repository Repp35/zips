"""Definição declarativa das tabelas e dos índices usados pelo bot."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS raid_entradas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
    entrou REAL NOT NULL, convite TEXT, criador TEXT
);
CREATE INDEX IF NOT EXISTS raid_entrada_user ON raid_entradas(guild_id,user_id,entrou);
CREATE INDEX IF NOT EXISTS raid_entrada_tempo ON raid_entradas(guild_id,entrou);

CREATE TABLE IF NOT EXISTS pedido_entregas (
    pedido_id INTEGER NOT NULL REFERENCES pedidos(id), estoque_id INTEGER NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, PRIMARY KEY(pedido_id, estoque_id, kind)
);
CREATE TABLE IF NOT EXISTS bots_adicionados (
    bot_id TEXT PRIMARY KEY,
    adicionado_por TEXT NOT NULL,
    adicionado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS webhooks_criadores (
    webhook_id TEXT PRIMARY KEY,
    criador_id TEXT NOT NULL,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS estoque (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    produto_key TEXT NOT NULL,
    email TEXT NOT NULL,
    senha TEXT NOT NULL,
    extra TEXT,
    vendido INTEGER NOT NULL DEFAULT 0,
    reservado INTEGER NOT NULL DEFAULT 0,
    reservado_em TEXT,
    reservado_por TEXT,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS estoque_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    produto_key TEXT NOT NULL,
    link TEXT NOT NULL,
    vendido INTEGER NOT NULL DEFAULT 0,
    reservado INTEGER NOT NULL DEFAULT 0,
    reservado_em TEXT,
    reservado_por TEXT,
    extra TEXT,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS pedidos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT,
    produto_key TEXT NOT NULL,
    valor REAL NOT NULL,
    quantidade INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    discord_user_id TEXT NOT NULL,
    thread_id TEXT,
    estoque_id INTEGER,
    checkout_key TEXT,
    checkout_descricao TEXT,
    checkout_email TEXT,
    entrega_status TEXT NOT NULL DEFAULT 'legacy',
    venda_anunciada INTEGER NOT NULL DEFAULT 0,
    pix_publicado INTEGER NOT NULL DEFAULT 0,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS precos (
    produto_key TEXT PRIMARY KEY,
    preco REAL NOT NULL,
    ativo INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS setup_messages (
    tipo TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads_ativas (
    thread_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tipo TEXT NOT NULL,
    produto_key TEXT,
    entrega_manual INTEGER NOT NULL DEFAULT 0,
    ticket_escolha TEXT,
    assumido_por TEXT,
    quantidade INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS banidos (
    user_id TEXT PRIMARY KEY,
    motivo TEXT,
    banido_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS admins (
    user_id TEXT PRIMARY KEY,
    adicionado_por TEXT NOT NULL,
    adicionado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS staff (
    user_id TEXT PRIMARY KEY,
    adicionado_por TEXT NOT NULL,
    adicionado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS canais_snapshot (
    channel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    category_id TEXT,
    topic TEXT,
    position INTEGER,
    permissoes TEXT,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS fechamentos_agendados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    fechar_em TEXT NOT NULL,
    motivo TEXT,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS entregas_confirmadas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id TEXT NOT NULL,
    estoque_id INTEGER NOT NULL,
    produto_key TEXT NOT NULL,
    entregue_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS restock_pedidos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    produto_key TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pendente',
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_restock_pedido_unico
    ON restock_pedidos (user_id, produto_key) WHERE status = 'pendente';
CREATE TABLE IF NOT EXISTS restock_interessados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    produto_key TEXT NOT NULL,
    preferencia TEXT NOT NULL DEFAULT 'notificar',
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
    desconto_pct INTEGER CHECK (desconto_pct IS NULL OR desconto_pct BETWEEN 1 AND 99),
    desconto_valor REAL CHECK (desconto_valor IS NULL OR desconto_valor > 0),
    tipo_desconto TEXT NOT NULL DEFAULT 'pct' CHECK (tipo_desconto IN ('pct', 'valor')),
    primeira_compra INTEGER NOT NULL DEFAULT 0,
    min_produto REAL NOT NULL DEFAULT 0.0,
    min_compras INTEGER NOT NULL DEFAULT 0,
    limite_usos INTEGER,
    limite_por_usuario INTEGER,
    usos_atuais INTEGER NOT NULL DEFAULT 0,
    validade TEXT,
    ativo INTEGER NOT NULL DEFAULT 1,
    cargos_permitidos TEXT,
    cargos_bloqueados TEXT,
    usuarios_permitidos TEXT,
    usuarios_bloqueados TEXT,
    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cupons_uso (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cupom_id INTEGER NOT NULL REFERENCES cupons(id),
    user_id TEXT NOT NULL,
    pedido_id INTEGER,
    usado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS avisos_estoque (
    user_id TEXT NOT NULL,
    produto_key TEXT NOT NULL,
    criado_em TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, produto_key)
);
CREATE TABLE IF NOT EXISTS produtos_anunciados (
    produto_key TEXT PRIMARY KEY,
    anunciado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS catalogo_loja (
    tipo TEXT NOT NULL,
    id TEXT NOT NULL,
    dados TEXT NOT NULL,
    atualizado_por TEXT NOT NULL,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tipo, id)
);
CREATE TABLE IF NOT EXISTS chats_travados (
    canal_id TEXT PRIMARY KEY, tinha_regra INTEGER NOT NULL,
    permite INTEGER NOT NULL, nega INTEGER NOT NULL, travado_por TEXT, travado_em TEXT
);
CREATE TABLE IF NOT EXISTS mensagens_membros (
    user_id TEXT PRIMARY KEY, total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vitrines_assinatura (
    tipo TEXT PRIMARY KEY, assinatura TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agente_conversas (
    canal_id INTEGER PRIMARY KEY, mensagens TEXT NOT NULL DEFAULT '[]',
    ligada INTEGER NOT NULL DEFAULT 0, atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS agente_diario (
    id INTEGER PRIMARY KEY AUTOINCREMENT, criado_em TEXT NOT NULL,
    ferramenta TEXT NOT NULL, resumo TEXT NOT NULL, plano TEXT, dono TEXT,
    desfazer TEXT, motivo TEXT, status TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_entregas_cliente ON entregas_confirmadas(discord_user_id, produto_key, estoque_id);
CREATE INDEX IF NOT EXISTS idx_cupom_uso ON cupons_uso(cupom_id, user_id);
DROP INDEX IF EXISTS idx_cupom_pedido;
DROP INDEX IF EXISTS idx_estoque_disponivel;
DROP INDEX IF EXISTS idx_links_disponivel;
CREATE INDEX IF NOT EXISTS idx_estoque_disponivel_v2 ON estoque (produto_key, id) WHERE vendido=0 AND reservado=0;
CREATE INDEX IF NOT EXISTS idx_links_disponivel_v2 ON estoque_links (produto_key, id) WHERE vendido=0 AND reservado=0;
CREATE UNIQUE INDEX IF NOT EXISTS idx_pedidos_payment_unique ON pedidos (payment_id) WHERE payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pedidos_status_thread ON pedidos (status, thread_id);
CREATE INDEX IF NOT EXISTS idx_pedidos_user_status ON pedidos (discord_user_id, status);
CREATE INDEX IF NOT EXISTS idx_threads_user_tipo ON threads_ativas (user_id, tipo, produto_key);
CREATE INDEX IF NOT EXISTS idx_pedidos_pending_thread ON pedidos (thread_id, id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_pedidos_user_product_status ON pedidos (discord_user_id, produto_key, status, criado_em);
CREATE INDEX IF NOT EXISTS idx_pedido_entregas_order_status ON pedido_entregas (pedido_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cupon_uso_order_unique ON cupons_uso (pedido_id) WHERE pedido_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_restock_status_created ON restock_pedidos (status, criado_em);
"""

SCHEMA_VERSION = 1
