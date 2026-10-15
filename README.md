# Webhook Bitrix24 → Linear

A rota existente `POST /bitrix-linear` continua a criar issues a partir de
`data.FIELDS`, copiar ficheiros do Bitrix Disk para Cloudflare R2 e acrescentar os
links à descrição. `GET /healthz` mantém o comportamento existente.

## Integração automática de tarefas

Configure um webhook **de saída** no Bitrix24 para **OnTaskUpdate**, com o endereço
`https://<serviço>/bitrix-task-event`. Guarde o token de aplicação fornecido pelo
Bitrix24 em `BITRIX_OUTGOING_TOKEN`. Este token é diferente do segredo do webhook
REST de entrada usado em `BITRIX_WEBHOOK_BASE`.

São aceites JSON e `application/x-www-form-urlencoded` (chaves como
`auth[application_token]` e `data[FIELDS_AFTER][ID]`). A autenticação compara o token
em tempo constante antes de consultar serviços externos. Outros eventos são ignorados.
Não se usam URLs, tokens REST nem domínios recebidos no payload.

O serviço consulta `tasks.task.get` com seleção explícita de `UF_CRM_TASK` e só
prossegue quando o responsável atual é **266 — João Cruz**. Como o evento pode
conter apenas IDs, consulta também `tasks.task.history.list`, com paginação, para
confirmar uma mudança real de outro responsável para 266. A criação inicial da
tarefa com 266 e uma edição sem mudança comprovada não bastam.

O histórico é considerado desde `BITRIX_TASK_EVENTS_SINCE` até ao `ts` do evento.
Defina o início da janela no momento de ativação. Isto permite recuperar uma
atribuição ocorrida nessa janela através de um evento posterior, mesmo após
reinício ou entrega fora de ordem. Não corresponde estritamente um registo de
histórico ao segundo de envio do webhook: uma edição posterior pode recuperar uma
atribuição ainda não processada. Tarefas já atribuídas a 266 antes dessa data não
são importadas. Eventos de tarefas atualmente atribuídas a outra pessoa são
ignorados. Acesso ao histórico de tarefas é obrigatório; uma falha devolve erro,
sem inferir mudanças a partir do responsável atual.

`ufCrmTask` deve conter exatamente um negócio `D_<id>` (contactos e outros tipos
são ignorados). Sem negócio ou com vários negócios distintos, a tarefa é ignorada
com motivo na resposta. `crm.deal.get` fornece o título, comentários, fase, valor,
moeda, empresa, contacto e responsável do negócio para a issue. A descrição inclui
o link do negócio e o ID/título da tarefa. A nova rota não importa anexos do negócio;
a cópia de anexos para R2 continua disponível na rota original.

### Persistência e repetição

Uma issue por **portal Bitrix24 + equipa Linear + tarefa**, mesmo após voltar a
atribuir a tarefa a João. Duas tarefas do mesmo negócio criam duas issues.
Uma transação SQLite reserva atomicamente a tarefa e um UUID v4 para a issue antes
de enviar a mutação. Pedidos concorrentes reutilizam a mesma reserva e o mesmo ID.
O Linear exige IDs únicos, impedindo que criem duas issues distintas.

Numa repetição, o serviço consulta primeiro esse ID no Linear, incluindo issues
arquivadas, e reconcilia o resultado com SQLite. Se não o encontrar, repete a criação
com o **mesmo ID**. Isto cobre timeout após a criação remota e falha antes de guardar
a confirmação local. A primeira reserva também fixa os dados enviados nas repetições.
Uma corrida pode devolver 502 ao pedido que perder; repetir esse evento reconcilia
a issue existente. Depois da reserva, a recuperação pode concluir mesmo se a tarefa
for entretanto reatribuída, pois já havia uma atribuição válida comprovada.

`BITRIX_TASK_STATE_DB` é obrigatório, absoluto e deve estar num **disco persistente**
gravável, com diretório previamente criado. Todos os workers devem partilhar o mesmo
ficheiro. Não usar filesystem efémero, NFS ou réplicas com discos independentes.
Faça backups e preserve a base em reinícios/deploys; perder/apagar a base perde as
reservas e pode criar duplicados. Para várias réplicas independentes, migrar esta
reserva transacional para uma base partilhada antes de escalar.

## Variáveis de ambiente

| Variável | Uso |
| --- | --- |
| `LINEAR_API_KEY` | Existente, chave de API Linear (valor sem `Bearer`). |
| `LINEAR_TEAM_ID` | Existente, UUID da equipa de destino. |
| `BITRIX_WEBHOOK_BASE` | Existente, URL REST autenticada terminada em `/`; precisa de permissões de tarefas, histórico e CRM, além de Disk para a rota original. |
| `BITRIX_OUTGOING_TOKEN` | **Nova, obrigatória para a nova rota.** Token de aplicação do webhook de saída. |
| `BITRIX_TASK_STATE_DB` | **Nova, obrigatória para a nova rota.** Caminho absoluto da base SQLite persistente, por exemplo `/var/data/bitrix-task-state.sqlite3`. |
| `BITRIX_TASK_EVENTS_SINCE` | **Nova, obrigatória para a nova rota.** Data ISO 8601 com fuso, por exemplo `2026-10-15T12:00:00+01:00`; substituir pelo momento real de ativação e manter entre deploys. |
| `LINEAR_JOAO_ASSIGNEE_ID` | **Nova, opcional.** UUID de João no Linear. Se ausente, a issue fica sem responsável; o ID Bitrix24 266 não é um ID Linear. |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID` | Existentes, credenciais e conta Cloudflare R2. |
| `R2_BUCKET_NAME`, `R2_PUBLIC_BASE_URL` | Existentes, bucket e URL pública para anexos. |

Sem as novas variáveis, a nova rota devolve 503; a rota original mantém a configuração
existente. Nunca guardar segredos no repositório. O plano gratuito atual no
`render.yaml` não fornece o disco persistente necessário: configurar um plano/disco
adequado antes de ativar esta integração. O manifesto de produção não foi alterado.

## Testes e validação

Requer Python 3.10 ou superior:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Os testes usam APIs simuladas e uma base SQLite temporária, sem criar issues reais.
Cobrem autenticação, JSON/form, deteção pelo histórico, paginação, limites temporais,
negócios ausentes/ambíguos, reservas concorrentes, persistência, timeouts antes/depois
da criação e regressão da rota original com anexos R2.

Antes de produção, validar num ambiente de teste com uma equipa Linear própria e
disco persistente: editar uma tarefa já atribuída antes da ativação (zero issues),
reatribuir uma tarefa de outro utilizador para 266 (uma issue), repetir o evento e
reiniciar o serviço (continua uma issue), e verificar um anexo pela rota original.
Em caso de 5xx, repetir o evento com o mesmo payload após resolver a causa. Não se
pressupõe reentrega automática pelo Bitrix24; configurar monitorização/replay no
ambiente de operação. O serviço não tem uma fila autónoma de retries.

Fontes: [OnTaskUpdate](https://apidocs.bitrix24.com/api-reference/tasks/events-tasks/on-task-update.html),
[tasks.task.get](https://apidocs.bitrix24.com/api-reference/tasks/tasks-task-get.html),
[histórico de tarefas](https://apidocs.bitrix24.com/api-reference/tasks/tasks-task-history-list.html),
[schema oficial Linear](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql).
