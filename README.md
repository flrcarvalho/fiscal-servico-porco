# 🐷 Fiscal de Serviço Porco

Monitor automático do Robôs365 com alertas no Telegram.

## O que faz

- Monitora o status do robô (LIGADO/DESLIGADO) a cada 2 minutos
- **Mensagem silenciosa** que vai sendo editada com todas as apostas do dia
- **Alerta com notificação** quando detecta: Robô Desligado, Saldo Insuficiente, Conta Limitada, Verificação ou Outros
- Suporte a múltiplos usuários e múltiplas licenças por usuário
- Cadastro pelo próprio Telegram com código de convite

---

## Setup no Railway + GitHub

### 1. Criar o bot no Telegram

1. Fale com [@BotFather](https://t.me/botfather)
2. `/newbot` → escolha o nome **Fiscal de Serviço Porco**
3. Guarde o **token** gerado

### 2. Descobrir seu Telegram ID

1. Fale com [@userinfobot](https://t.me/userinfobot)
2. Ele responde com seu ID numérico — guarde ele

### 3. Subir no GitHub

1. Crie um repositório no GitHub (pode ser privado)
2. Suba todos os arquivos deste projeto

### 4. Deploy no Railway

1. Acesse [railway.app](https://railway.app) e faça login
2. **New Project → Deploy from GitHub repo** → selecione seu repositório
3. Vá em **Variables** e adicione:

```
TELEGRAM_BOT_TOKEN = seu_token_do_botfather
ADMIN_TELEGRAM_ID  = seu_id_numerico
CHECK_INTERVAL_SECONDS = 120
```

4. Railway vai fazer o build automaticamente com o Dockerfile

---

## Como usar

### Como admin (você)

**Gerar código de convite:**
```
/gerar_convite
/gerar_convite 5   ← gera 5 códigos de uma vez
```

### Como usuário

1. Fale com o bot no Telegram
2. `/start` → insira o código de convite
3. Informe e-mail e senha da conta Robôs365
4. Dê um nome para a licença
5. O bot começa a monitorar!

**Comandos disponíveis:**
```
/start           — Cadastro / Menu principal
/add_licenca     — Adicionar nova licença
/minhas_licencas — Ver licenças cadastradas
/remover         — Remover uma licença
/status          — Ver status atual de todas as licenças
/cancelar        — Cancelar operação em andamento
```

---

## Mensagens no Telegram

### Mensagem de resumo (editada silenciosamente, sem notificação)
```
🐷 Fiscal de Serviço Porco
📋 Conta Principal
Robô: 🟢 LIGADO
🕐 Última checagem: 28/05 - 21:30

Apostas do dia:
✅ Radka Zelnickova vs Giulia Popa — 28/05 - 16:04
✅ Franziska Sziedat vs Laura Mair — 28/05 - 15:49
🔥 Jaime Faria vs Frances Tiafoe — 28/05 - 16:47
📴 Luisina Giovannini vs Nadia Podoroska — 28/05 - 21:05
```

### Alerta (com notificação sonora)
```
🚨 📴 ROBÔ DESLIGADO
📋 Conta Principal

📴 Luisina Giovannini vs Nadia — 28/05 - 21:05
   Status: Robô Desligado

🕐 28/05 - 21:07
```

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token do @BotFather |
| `ADMIN_TELEGRAM_ID` | ✅ | Seu ID no Telegram |
| `CHECK_INTERVAL_SECONDS` | ❌ | Intervalo de checagem (padrão: 120) |
| `DB_PATH` | ❌ | Caminho do banco SQLite (padrão: fiscal.db) |
