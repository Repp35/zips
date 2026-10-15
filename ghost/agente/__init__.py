"""Ghost Agent: uma IA que age no bot por ferramentas. Só o dono usa. Credenciais não entram nas ferramentas.

Módulos:
- provedor.py: fala com o modelo (Groq por padrão; OpenRouter, Gemini ou endpoint compatível). Trocar de provedor = AGENT_PROVIDER + AGENT_API_KEY no .env.
- Antes de responder ou chamar ferramentas, classifica semanticamente se a fala recente do dono foi dirigida ao agente; menção não é obrigatória.
- ferramentas.py: lista fechada de ações. Ação de leitura roda direto; ação de escrita só vira pedido pendente.
- loop.py: conversa com o modelo, executa leituras, guarda os pedidos de escrita e interrompe no limite de passos.
- Os pedidos de escrita só são gravados quando o dono clica em Confirmar (cog bot/cogs/agente.py).
"""
