# Webhook Bitrix24 → Linear

O **Bitrix24 é a plataforma principal de gestão das tarefas**. Quando o responsável
muda para **João Cruz (ID 266)**, a integração cria uma issue no Linear para o
desenvolvimento externo, usando os dados do negócio CRM associado à tarefa.

Não existe sincronização de estados, conclusões ou atualizações entre as plataformas.
A conclusão da tarefa no Bitrix24 é feita **internamente, após testes e validação do
desenvolvimento externo**. A integração nunca conclui nem modifica tarefas Bitrix24.

## Configuração do evento

Configure um webhook **de saída** no Bitrix24 para **OnTaskUpdate**, com o endereço
`https://<serviço>/bitrix-task-event`. Guarde o token de aplicação fornecido pelo
Bitrix24 em `BITRIX_OUTGOING_TOKEN`. É diferente do segredo REST de entrada usado em
`BITRIX_WEBHOOK_BASE`.

A rota aceita JSON e `application/x-www-form-urlencoded`, incluindo as chaves
`auth[application_token]` e `data[FIELDS_AFTER][ID]`. Valida o token em tempo constante
antes de consultar serviços externos e ignora outros eventos. Usa apenas o endpoint
REST configurado, nunca URLs ou credenciais recebidas no evento.

O fluxo é:

1. Consultar `tasks.task.get`, selecionando explicitamente `UF_CRM_TASK`, e confirmar
   que o responsável atual é 266.
2. Consultar `tasks.task.history.list`, com paginação, para confirmar uma mudança de
   outro responsável para 266 **no timestamp `ts` do evento**, com precisão de segundos.
   Uma atribuição antiga não transforma uma edição de título, estado ou conclusão
   numa nova atribuição. A criação inicial de uma tarefa já atribuída a 266 é ignorada.
3. Extrair exatamente um negócio `D_<id>` de `ufCrmTask` e consultar `crm.deal.get`.
   Sem negócio ou com vários negócios distintos, devolver o motivo e ignorar a tarefa.
4. Enviar diretamente **uma mutação `issueCreate`**, com título e comentários do
   negócio, fase CRM, valor, moeda, empresa, contacto, responsável do negócio, link do
   negócio e identificação da tarefa. A fase é apenas texto de contexto na descrição;
   não determina o estado da issue. O responsável Linear é opcional.
5. Após a criação confirmada da issue, consultar os anexos CRM e transferi-los
   individualmente para o armazenamento nativo do Linear, como descrito abaixo.

O histórico deve estar acessível ao utilizador do webhook REST. A comparação usa o
instante do evento, não o da receção, pelo que uma entrega atrasada mantém a referência
original. Se o Bitrix24 apresentar timestamps diferentes entre evento e histórico,
a mudança não é considerada comprovada; validar esta correspondência no portal antes
de ativar. Alterações no mesmo segundo podem partilhar a mesma referência temporal,
mas usam o mesmo ID de issue. Se a tarefa já tiver sido reatribuída a outra pessoa
quando for consultada, o evento é ignorado. `BITRIX_TASK_EVENTS_SINCE` limita a
ativação; eventos anteriores são ignorados.

## UUID determinístico e erros

Não há consultas ao Linear para procurar issues existentes, base de dados local,
ficheiros de estado, reservas, cache ou necessidade de disco persistente.

O ID é calculado a partir de uma tupla JSON com namespace `bitrix-linear-task-v1`,
portal Bitrix24 em minúsculas, equipa Linear normalizada e ID inteiro da tarefa.
Usam-se os primeiros 16 bytes de SHA-256, com os bits de versão/variante ajustados ao
**formato UUID v4 exigido pelo schema Linear**. O valor é determinístico, não aleatório.
Reentregas, pedidos simultâneos, reinícios e novas atribuições da mesma tarefa à mesma
equipa reutilizam o mesmo ID. Outra tarefa, portal ou equipa produz outro ID; mudar
credenciais REST, negócio, título ou descrição não altera a identidade da tarefa.

Isto **reduz o risco de duplicados**, sem garantir processamento exatamente uma vez.
Se o Linear rejeitar um ID existente, a resposta continua a ser um erro 502; a
integração não consulta a issue nem transforma um conflito presumido em sucesso.
Erros HTTP/GraphQL, falhas de transporte e criação não confirmada também são
reportados. Um timeout pode ocorrer após a criação remota: repetir o evento usa o
mesmo ID, mas não confirma o resultado anterior. Não se fazem tentativas com um ID
alternativo. Cada tentativa volta a consultar os dados atuais do negócio.

Um evento inválido devolve 400, token inválido 403, formato não suportado 415,
configuração incompleta 503 e falhas nas APIs 502. Não existe fila autónoma de retries;
monitorização e eventual replay devem ser geridos operacionalmente.

## Rotas existentes e Render Free

`POST /bitrix-linear` mantém a integração existente: cria issues a partir de
`data.FIELDS`, procura o responsável por email quando fornecido, copia anexos do
Bitrix Disk para **Cloudflare R2** e acrescenta os links à descrição. Esta rota legada
não muda; a nova transferência nativa aplica-se apenas a `/bitrix-task-event`.
`GET /healthz` mantém o comportamento existente.

O `render.yaml` mantém **`plan: free`**, sem disco persistente. As novas variáveis são
configuradas no ambiente do serviço, sem guardar segredos no repositório. Este PR não
faz merge, deploy nem alterações no serviço de produção.

## Variáveis de ambiente

| Variável | Uso |
| --- | --- |
| `LINEAR_API_KEY` | Chave de API Linear, sem `Bearer`. |
| `LINEAR_TEAM_ID` | UUID da equipa de destino. |
| `BITRIX_WEBHOOK_BASE` | URL REST autenticada, terminada em `/`; permissões de tarefas, histórico e CRM, além de Disk para a rota original. |
| `BITRIX_OUTGOING_TOKEN` | Obrigatória na nova rota: token de aplicação do webhook de saída. |
| `BITRIX_TASK_EVENTS_SINCE` | Obrigatória na nova rota: data ISO 8601 com fuso, por exemplo `2026-10-15T12:00:00+01:00`; usar o momento real de ativação e manter entre deploys. |
| `LINEAR_JOAO_ASSIGNEE_ID` | Opcional: UUID de João no Linear; se ausente, a issue fica sem responsável. O ID Bitrix24 266 não é um ID Linear. |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID` | Credenciais e conta Cloudflare R2 existentes. |
| `R2_BUCKET_NAME`, `R2_PUBLIC_BASE_URL` | Bucket e URL pública dos anexos existentes. |

## Testes e validação

Requer Python 3.10 ou superior:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Os testes simulam as APIs e não criam issues reais. Cobrem autenticação, JSON/form,
histórico paginado, mudança efetiva, edição/conclusão sem atribuição, negócios
inválidos/ambíguos, identidade determinística entre tentativas e processos, concorrência,
timeouts e erros Linear sem consultas prévias, além da regressão dos anexos R2.

Antes de ativar, validar num ambiente de teste: mudar uma tarefa de outro utilizador
para 266 (uma issue com os dados CRM), editar o estado ou concluir a tarefa (nenhuma
nova chamada ao Linear), repetir o evento original (mesmo UUID, conflito reportado se
o Linear o rejeitar) e verificar um anexo na rota original. A conclusão funcional
continua a ser feita internamente no Bitrix24 após os testes do desenvolvimento.

Fontes: [OnTaskUpdate](https://apidocs.bitrix24.com/api-reference/tasks/events-tasks/on-task-update.html),
[tasks.task.get](https://apidocs.bitrix24.com/api-reference/tasks/tasks-task-get.html),
[histórico de tarefas](https://apidocs.bitrix24.com/api-reference/tasks/tasks-task-history-list.html),
[schema oficial Linear](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql).


## Prioridade do negócio CRM

Na criação da issue, `/bitrix-task-event` lê exclusivamente
`UF_CRM_DEAL_1662136383881` de `crm.deal.get` (equivalente a
`ufCrm_DEAL_1662136383881` em `crm.item.get`) e inclui `priority` no
`IssueCreateInput`:

| Opção Bitrix24 (número ou string) | Prioridade Linear |
| --- | --- |
| `162` — Crítica | `1` — Urgent |
| `106` — Alta | `2` — High |
| `108` — Média | `3` — Medium |
| `110` — Baixa | `4` — Low |
| Ausente, null, vazio ou desconhecido | `0` — No priority |

A correspondência foi fornecida e confirmada no negócio 20622: opção `108`
corresponde a «Média». Não se usa `UF_CRM_DEAL_1675256812888`, a prioridade da
tarefa, o título, a descrição ou `searchContent`. A prioridade é aplicada apenas
na criação; alterações posteriores do negócio não atualizam issues existentes.
Não são necessárias novas consultas à API nem variáveis de ambiente.

## Título com Assunto: identificação pelo portal

O título usa `TITLE` do negócio, por exemplo `Ticket_14220`, seguido de ` - Assunto`
quando o campo estiver preenchido. Nunca se usam o título da tarefa, `SUBJECT` ou
`searchContent` para obter o assunto.

O código técnico de **Assunto é personalizado por portal** e não está identificado
nos dados fornecidos ou no repositório. Não foi inventado um código nem acrescentada
uma variável com um mapeamento presumido. Em cada evento válido, `crm.deal.fields`
consulta os metadados reais: deve existir exatamente um campo `UF_CRM_*` com rótulo
`Assunto` em `title`, `formLabel` ou `listLabel`, do tipo `string`, não múltiplo.
Esse código retornado pelo portal é usado para ler o valor em `crm.deal.get`.
Os rótulos multilingues são suportados. A consulta tem um orçamento de 5 segundos.

Se o campo não existir, for ambíguo, tiver tipo incompatível ou a consulta falhar,
usa-se apenas o título do negócio e regista-se um aviso sanitizado. Valores vazios
não acrescentam separador. Se o assunto já estiver contido no título (ignorando
maiúsculas e espaços repetidos), não é acrescentado novamente.
**Validação pendente:** confirmar no portal real o código de Assunto e o rótulo
exposto por `crm.deal.fields`. O código `UF_CRM_99999` nos testes é apenas sintético.

## Anexos nativos no Linear

Apenas depois de a issue ser criada com sucesso, consulta-se
`crm.item.get` com `entityTypeId=2` e o ID do negócio obtido de `UF_CRM_TASK`.
Lê-se o campo `ufCrm_1662137274943` (equivalente a `UF_CRM_1662137274943`).
São aceites zero, um ou vários objetos de ficheiro. O download usa `urlMachine`;
**estes IDs de ficheiro CRM nunca são enviados a `disk.file.get`**.

Para cada ficheiro:

1. Validar HTTPS, porta 443 e domínio exatamente igual ao portal configurado.
   Não são admitidas credenciais na autoridade do URL, fragmentos ou outro domínio.
   Não se seguem redirecionamentos, mesmo dentro do portal; não se reenviam chaves
   Linear nem credenciais REST por cabeçalhos ao ficheiro.
2. Descarregar em blocos de 64 KiB, com limite de **8 MiB por ficheiro**. Validar
   MIME, tamanho declarado e real. HTML, XML/SVG, JSON, corpos vazios e respostas
   comprimidas são rejeitados, incluindo erros HTML/JSON disfarçados de PDF/binário.
   Não se faz análise antivírus nem validação completa de todos os formatos.
3. Chamar `fileUpload(contentType: String!, filename: String!, size: Int!)`.
   Obter `uploadFile { uploadUrl assetUrl headers { key value } }`.
4. Fazer PUT dos bytes para `uploadUrl`, copiando todos os cabeçalhos fornecidos,
   além de `Content-Type` e `Cache-Control` conforme o guia oficial. Não são
   enviadas credenciais da API Linear ao endpoint de armazenamento.
5. Associar o **`assetUrl` definitivo** com
   `attachmentCreate(input: AttachmentCreateInput!)`, fornecendo `issueId`, `title`
   (nome do ficheiro), `url` e um ID de anexo determinístico. O URL temporário do
   upload nunca é associado à issue. Não se pede publicação pública dos ficheiros.

O nome original é obtido de `Content-Disposition`, incluindo UTF-8, ou de `name`
quando disponível. Removem-se caminhos e caracteres de controlo; existe um nome
seguro de fallback. Não há uso de R2 nesta funcionalidade.

Há no máximo **8 ficheiros tentados**, **24 MiB aceites por evento** e **45 segundos
para a fase inteira de anexos**, incluindo a consulta CRM. Os ficheiros são tratados
sequencialmente, sem acumular todos os conteúdos em memória. O timeout de I/O é
15 segundos, com 5 segundos para ligação, dentro do orçamento global. Os limites
são constantes no código; **não há novas variáveis de ambiente**.

Falhas isoladas são registadas apenas por etapa, sem texto da exceção, nomes,
URLs, cabeçalhos ou corpos das APIs. As mensagens de transporte HTTPX/HTTPCore são
suprimidas no contexto dos pedidos privados para impedir exposição de tokens ou
URLs assinados mesmo com logging DEBUG. A resposta inclui apenas contadores
`attachments: {attached, failed, skipped}`. A falha dos anexos mantém a issue e o
sucesso da criação; ficheiros acima dos limites ficam por transferir.

### Reenvios e limites do Render Free

O UUID da issue mantém-se inalterado. Se uma reentrega for rejeitada pelo Linear
na criação por ID já existente, não se descarregam nem reenviam anexos. IDs de
ficheiro repetidos no mesmo evento são ignorados. O ID do anexo é determinístico
por issue, campo CRM e ID de ficheiro, reduzindo duplicados também em concorrência.
Não há consultas prévias ao Linear para procurar issues/anexos nem estado local.

Sem persistência, não se garante recuperação automática de anexos falhados ou de
interrupções após a criação da issue. Um reenvio pode falhar na criação da issue
existente antes de voltar a tratar os anexos. Um timeout após o PUT pode deixar um
ficheiro órfão no armazenamento Linear; o ID determinístico do anexo não evita
uploads órfãos. A reparação de transferências parciais exige intervenção operacional.

No Render Free, cold starts podem atrasar a resposta e provocar reenvios do Bitrix24.
O processamento é síncrono e pode exceder o timeout do emissor/proxy, apesar do
orçamento da fase de anexos. Suspensões/reinícios podem interromper a transferência;
requests concorrentes multiplicam o consumo limitado por pedido. Não se adiciona
fila, base de dados, disco persistente ou plano pago. Validar no portal real os
URLs `urlMachine`, metadados de nome/MIME e acesso aos anexos privados no Linear.

Referências oficiais verificadas para este PR:
[upload Linear](https://linear.app/developers/how-to-upload-a-file-to-linear),
[attachments Linear](https://linear.app/developers/attachments),
[schema GraphQL Linear](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql),
[metadados de campos CRM](https://apidocs.bitrix24.com/api-reference/crm/deals/crm-deal-fields.html).
