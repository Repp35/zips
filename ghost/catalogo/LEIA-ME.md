# Catálogo da loja

A fonte da verdade é uma pasta: `produtos/`, com um arquivo JSON por produto. O nome do arquivo é o `id`.
Tudo que é do produto fica no próprio arquivo: preço, canal, modelo de entrega, mensagem do carrinho,
opção do menu e o bloco da vitrine onde ele aparece. Não existe modelo nem vitrine à parte.

O banco não guarda produto. Criar, editar e apagar produto gravam ou apagam o arquivo (pelo painel
`/gerenciar` → Produto, ou editando a pasta). O código confere tudo com `normalizar()` em `__init__.py`.
Um erro de estrutura impede o bot de subir e mostra o arquivo com problema.

## Produto

```json
{
  "id": "netflix_fa",
  "nome": "Netflix FA",
  "preco": 11.6,
  "ordem": 1,                       // ordem dentro da vitrine (menor aparece primeiro)
  "ativo": true,                    // false tira o produto do menu e do estoque exibido
  "anunciar": true,                 // false não posta "Novo produto" no canal de avisos
  "canal": null,                    // ID numérico do canal da vitrine; null usa o canal salvo da vitrine
  "imagem": "https://...",
  "garantia": "7 dias após a entrega.",   // opcional: aparece no carrinho
  "sem_estoque": "avisar",          // opcional: avisar | manual (senão vale o de modelo)
  "botao": {"label": "Comprar", "style": 2, "emoji": "@cart"},  // opcional: só em vitrine de painel com 1 produto
  "select": {
    "rotulo": "Netflix FA",         // nome na opção do menu
    "descricao": "{preco} | {estado}",   // texto da opção; {preco} e {estado} são preenchidos sozinhos
    "emoji": "<:netflix:123>"       // opcional: emoji da opção
  },
  "modelo": {
    "estoque": "conta",             // conta (login:senha) | link | null (sem estoque, entrega manual)
    "entrega": "automatica",        // automatica | manual
    "sem_estoque": "avisar",        // avisar | manual
    "pagamento": "Pagamento via PIX — entrega automática após confirmação.",
    "pagamento_manual": "Pagamento via PIX — entrega **manual** realizada por um membro da equipe."
  },
  "mensagem": {
    "automatico": [ ...blocos do carrinho... ],
    "manual": [ ...blocos do carrinho... ]
  },
  "vitrine": { ...bloco da vitrine... }
}
```

Campos aceitos (qualquer outro é recusado): `id`, `nome`, `preco`, `ordem`, `ativo`, `anunciar`, `canal`,
`imagem`, `garantia`, `select`, `botao`, `sem_estoque`, `modelo`, `mensagem`, `vitrine`.

## Vitrine

O bloco `vitrine` vira a mensagem publicada no Discord. Ele é montado a partir do **produto ativo de menor
`ordem`** dentro da vitrine. Os outros produtos da mesma vitrine carregam uma cópia igual do bloco, e o código
avisa se alguma cópia diferir. Se nenhum produto da vitrine estiver ativo, ela usa o de menor `ordem` mesmo assim.

- O ID da vitrine é `vitrine.id` (ex.: `FA`). Produtos com o mesmo ID dividem a vitrine.
- Quando o último produto de uma vitrine é apagado, a mensagem da vitrine é retirada.
- Tipos:
  - `selecao`: embed com menu de produtos. Campos: `custom_id`, `placeholder`, `imagem`, `titulo`, `descricao`, `cor`.
  - `painel`: mensagem com blocos. Com 1 produto, tem botão de compra; com mais, tem menu. Usa `blocos`,
    `seletor` (`custom_id` e `placeholder`, quando há mais de um produto) e `acoes` (botões opcionais).
  - `selecao_simples`: menu de opções fixas, sem produto. Só existe no sistema (ver abaixo).
- `nome_setup` é o nome da vitrine no `/setup`. `publicar: false` retira a mensagem do Discord.

Blocos de vitrine do tipo `painel`: `texto` (`"texto": "..."`), `linha`, `imagem` (`"url"`), `estoque` (rodapé
com o estoque do produto).

Canal: o campo `canal` do produto que abre a vitrine tem prioridade. Se ele for `null`, vale o canal salvo da
vitrine (`/setup`, config `<tipo>_channel_id`). Se nenhum existir, a vitrine não é publicada.

## Vitrines do sistema

`SUPORTE` e `PEDIDO_RESTOCK` não têm produto. Por isso não vêm de arquivo de produto: ficam em
`VITRINES_DO_SISTEMA`, no `__init__.py`. Para mudar o texto ou as opções delas, edite esse dicionário.

## Modelo e carrinho

O modelo (`modelo`) define como o estoque é usado, se a entrega é automática ou manual e os textos de pagamento.
A mensagem do carrinho (`mensagem.automatico` e `mensagem.manual`) é a sequência de blocos: textos, linhas,
imagem e botões. Botões aceitos: `pagar`, `lote`, `cupom`, `cancelar`. Para tirar lote ou cupom, remova o botão.

Blocos do carrinho: `texto` (`"texto"`), `linha`, `imagem` (sem `url` usa a imagem do produto), `botoes`
(`"itens"`: cada item com `acao`, `label`, `style` e `emoji`; `label_com_cupom` no botão de cupom).

Placeholders do carrinho: `{titulo}`, `{preco}`, `{valor}`, `{cupom}`, `{cupom_codigo}`, `{estoque}`,
`{garantia}`, `{pagamento}`, `{quantidade}`.

Placeholders da vitrine e do carrinho: `{E.nome}` vira o emoji de asset do bot (ex.: `{E.cardbox}`; os nomes estão
em `ghost/emojis.py`). Campos de emoji aceitam `@nome` (asset), `<:nome:id>` (emoji de marca) ou unicode.

## Como adicionar um produto

1. Crie `produtos/<id>.json` com o formato acima. O `id` vai de 2 a 40 letras minúsculas, números ou `_`.
2. Reinicie o bot, ou use `/gerenciar` → Produto. Se houver erro, o bot mostra o arquivo e o motivo.
3. O produto entra na vitrine (publicada no canal configurado) e é anunciado uma única vez, se `anunciar` for `true`.

Para tirar um produto de circulação, use `"ativo": false`. Para apagar, remova o arquivo (ou use `/gerenciar`):
o estoque do produto é apagado junto.
