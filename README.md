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
   https://discord.com/oauth2/authorize?client_id=SEU_CLIENT_ID&scope=bot+applications.commands&permissions=360777403392
   ```

   (permissões: ver canais, enviar mensagens, inserir links, mencionar @everyone, criar threads privadas, enviar mensagens em threads e gerenciar threads; `applications.commands` habilita o `/code-review`.)

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
./dr me           # troca qual é o seu usuário (recebe os testes e uma cópia de todo envio)
./dr delete 1
./dr history      # últimos envios (enviado / erro / pulado)
./dr reviews      # code reviews em aberto (--all inclui os finalizados)
./dr review-channel  # canal do painel de code review (rode de novo para republicar o painel)
```

Com o bot rodando, canais, cargos e membros aparecem como opções no assistente. Sem ele, dá para informar os IDs manualmente: ative o Modo Desenvolvedor no Discord (Configurações → Avançado) e use "Copiar ID".

No campo de mensagem, que aceita várias linhas, pressione **Esc e depois Enter** para concluir.

## Code review

Funciona como um sistema de tickets. Com `./dr review-channel` você escolhe o canal de code review, e o bot publica nele um painel com o botão **📝 Registrar code review**.

1. A pessoa clica no botão (ou usa **`/code-review`**) e preenche o formulário: tipo (implementação nova, ajuste, correção de bug, voltou de ajuste do code review, voltou/reaberta, outro), link da tarefa, link do PR e descrição.
2. O bot abre uma **thread privada** só com o autor e o revisor (o usuário de `./dr me`), publica o card nela e manda o code review no privado do revisor. O canal fica limpo, só com o painel.
3. No privado, o revisor tem os botões **🔍 Em revisão**, **✅ Aprovado**, **✏️ Ajustes solicitados**, **💬 Revisado** e **🚫 Cancelado**. Cada clique atualiza o card e marca o autor na thread.
4. Com ajustes solicitados, o card ganha o botão **🔁 Ajustes feitos**. O autor clica, conta o que mudou (opcional), e o code review volta para o revisor como uma nova rodada.
5. Ao aprovar ou cancelar, a thread é arquivada e trancada. Se o status mudar depois, ela reabre.

Os botões continuam funcionando depois de reiniciar o bot. Só o revisor muda o status, e só o autor (ou o revisor) devolve após ajustes.

Configuração do canal no Discord:

- O bot precisa de **Criar threads privadas**, **Enviar mensagens em threads** e **Gerenciar threads** no canal (o link do passo 1 já pede). Sem permissão para criar threads, o card vai para o próprio canal, como antes.
- Para o canal ter só o painel, tire de @everyone a permissão **Enviar mensagens** nele e deixe **Enviar mensagens em threads** ligada, para o pessoal conversar no ticket.
- Se apagarem o painel, rode `./dr review-channel` de novo e escolha o mesmo canal.

Se o `/code-review` ou o painel não aparecerem, convide o bot de novo com o link do passo 1 e reinicie o `./dr bot`.

## Comportamento

- **Fuso horário:** `TIMEZONE` no `.env` (padrão `America/Sao_Paulo`).
- **Bot desligado no horário:** ao voltar, ele envia se o atraso for menor que `GRACE_MINUTES` (padrão 30). Se for maior, pula aquele envio e registra no histórico, para não disparar lembretes velhos.
- **Privado:** o bot só consegue mandar DM para quem está num servidor onde ele também está. Quem bloqueou DMs de membros do servidor não recebe. Nesse caso o histórico mostra `parcial` com o nome de quem falhou, e os outros recebem normalmente.
- **Cópia para você:** todo envio real de qualquer lembrete também chega no seu privado (o usuário definido em `./dr me`). Se você já era destinatário, recebe só uma vez; se não era, a mensagem vem marcada como "📋 Cópia do lembrete".
- **Lembrete "uma única vez":** depois do envio, fica como `concluído`.
- **Mensal no dia 29–31:** meses sem esse dia são pulados. Para "último dia do mês", use `L`.
- Os dados ficam em `data/daily_remember.db` (SQLite).

## Deixar rodando 24/7

O bot só envia enquanto `./dr bot` estiver rodando. Se ele rodar no seu Mac, os lembretes não saem com o Mac desligado ou em repouso. Para algo confiável, rode o bot num servidor pequeno (uma VM, Railway, Fly.io etc.) com o mesmo comando. A CLI precisa acessar o mesmo arquivo `data/daily_remember.db`, então nesse caso use a CLI pelo SSH no próprio servidor.

Para rodar em segundo plano localmente:

```bash
nohup ./dr bot > bot.log 2>&1 &
```
