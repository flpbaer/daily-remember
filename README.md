# daily-remember

Bot de Discord que envia lembretes agendados para o squad, configurado pelo terminal.

- Agendas: todo dia, dias úteis, dias específicos da semana, mensal, uma única vez ou expressão cron.
- Várias vezes ao dia: horários fixos (ex.: `09:00, 14:00, 19:00`) ou intervalo (ex.: de hora em hora, das 9h às 18h), em todos os dias, dias úteis ou dias escolhidos.
- Destino por lembrete:
  - **Privado** (padrão): o bot manda uma DM para cada pessoa escolhida. Você pode escolher pessoas ou um cargo inteiro, e o cargo é expandido na hora do envio, então quem entrar nele depois também recebe.
  - **Canal**: anúncio geral num canal, com menções (`@everyone`, `@here`, cargos, membros).
  - **Os dois.**
- Mensagem com título opcional (card destacado) e links como botões.
- Variáveis na mensagem: `{nome}` (nome da pessoa no privado, "pessoal" no canal), `{data}`, `{dia_semana}`, `{hora}`.
- Pausar, retomar, editar, enviar agora (teste) e histórico de envios.
- **O bot e a CLI são processos separados.** O bot lê o banco a cada 15s, então você cria e edita lembretes com ele rodando, sem reiniciar e sem afetar os outros.

## 1. Criar o bot no Discord

1. Acesse <https://discord.com/developers/applications> → **New Application**.
2. Aba **Bot** → **Reset Token** → copie o token.
3. Na mesma aba, ligue **Server Members Intent**. É necessário para listar os membros na CLI e para enviar no privado a todos de um cargo. Sem ele, use `MEMBERS_INTENT=false` no `.env` e informe as pessoas por ID.
4. Aba **OAuth2** → copie o **Client ID** e abra no navegador (troque `SEU_CLIENT_ID`):

   ```
   https://discord.com/oauth2/authorize?client_id=SEU_CLIENT_ID&scope=bot&permissions=150528
   ```

   (permissões: ver canais, enviar mensagens, inserir links e mencionar @everyone.)

## 2. Instalar

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # cole o token em DISCORD_TOKEN
```

## 3. Usar

Inicie o bot em um terminal e deixe aberto:

```bash
./dr bot
```

Em outro terminal, gerencie os lembretes:

```bash
./dr              # menu interativo
./dr add          # cria um lembrete (assistente passo a passo)
./dr list         # lista com status, próximo e último envio
./dr show 1       # pré-visualiza o lembrete #1
./dr edit 1
./dr pause 1
./dr resume 1
./dr test 1       # manda só no SEU privado, para ver como ficou (ninguém mais recebe)
./dr send 1       # envia agora para todos os destinatários, sem mexer na agenda
./dr me           # troca qual é o seu usuário (destino dos testes)
./dr delete 1
./dr history      # últimos envios (enviado / erro / pulado)
```

Com o bot rodando, canais, cargos e membros aparecem como opções no assistente. Sem ele, dá para informar os IDs manualmente: ative o Modo Desenvolvedor no Discord (Configurações → Avançado) e use "Copiar ID".

No campo de mensagem, que aceita várias linhas, pressione **Esc e depois Enter** para concluir.

## Comportamento

- **Fuso horário:** `TIMEZONE` no `.env` (padrão `America/Sao_Paulo`).
- **Bot desligado no horário:** ao voltar, ele envia se o atraso for menor que `GRACE_MINUTES` (padrão 30). Se for maior, pula aquele envio e registra no histórico, para não disparar lembretes velhos.
- **Privado:** o bot só consegue mandar DM para quem está num servidor onde ele também está. Quem bloqueou DMs de membros do servidor não recebe. Nesse caso o histórico mostra `parcial` com o nome de quem falhou, e os outros recebem normalmente.
- **Lembrete "uma única vez":** depois do envio, fica como `concluído`.
- **Mensal no dia 29–31:** meses sem esse dia são pulados. Para "último dia do mês", use `L`.
- Os dados ficam em `data/daily_remember.db` (SQLite).

## Deixar rodando 24/7

O bot só envia enquanto `./dr bot` estiver rodando. Se ele rodar no seu Mac, os lembretes não saem com o Mac desligado ou em repouso. Para algo confiável, rode o bot num servidor pequeno (uma VM, Railway, Fly.io etc.) com o mesmo comando. A CLI precisa acessar o mesmo arquivo `data/daily_remember.db`, então nesse caso use a CLI pelo SSH no próprio servidor.

Para rodar em segundo plano localmente:

```bash
nohup ./dr bot > bot.log 2>&1 &
```
