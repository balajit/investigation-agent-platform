build a generic Investigation Agent platform whose evidence adapters happen to be Elastic, Oracle, and source repositories.
The important architectural decision is to separate investigation reasoning from evidence acquisition.
1. The core architecture
                         ┌──────────────────────┐
                         │   Investigation API   │
                         │                      │
User Issue ─────────────►│ session_id           │
                         │ application          │
                         │ problem description  │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ Investigation Agent  │
                         │                      │
                         │ Planner              │
                         │ Correlator           │
                         │ Hypothesis Manager   │
                         │ Evidence Evaluator   │
                         │ Root-Cause Analyzer  │
                         └──────────┬───────────┘
                                    │
                     ┌──────────────┼──────────────┐
                     │              │              │
                     ▼              ▼              ▼
              Observability     State Store     Code Intelligence
                  Adapter          Adapter          Adapter
                     │              │              │
                     ▼              ▼              ▼
                  Elastic         Oracle       Git/Code Search
The agent doesn't know Elastic, Oracle, or Git.
It knows:
"I need runtime evidence."
"I need application state."
"I need implementation evidence."
That's what makes it reusable.

2. Don't make the agent itself responsible for everything
I'd split the system into five logical components.
A. Investigation Orchestrator
Owns the lifecycle:
CREATE
  ↓
CONTEXTUALIZE
  ↓
INVESTIGATE
  ↓
CORRELATE
  ↓
HYPOTHESIZE
  ↓
VERIFY
  ↓
CONCLUDE
It manages the investigation state and controls what the agent is allowed to do.
B. Investigation Agent
This is the reasoning component.
It answers:
* What do I know?
* What don't I know?
* What evidence should I obtain next?
* Which hypothesis does the evidence support?
* What contradictory evidence exists?
* What code should I inspect?
* Is the root cause sufficiently established?
C. Evidence Adapters
These hide infrastructure.
ObservabilityEvidenceProvider
StateEvidenceProvider
CodeEvidenceProvider
Implementations:
ElasticEvidenceProvider
OracleEvidenceProvider
GitEvidenceProvider
Later:
OpenSearchEvidenceProvider
PostgresEvidenceProvider
GitHubEvidenceProvider
GitLabEvidenceProvider
without changing the investigation agent.
D. Correlation Engine
This is probably the most important non-LLM component.
Given:
session_id = S123
it discovers:
S123
 │
 ├── request_id R1
 ├── correlation_id C1
 ├── transaction_id T1
 ├── order_id O1
 └── job_id J1
Then uses those identifiers to retrieve related evidence.
I would make correlation a first-class capability rather than relying entirely on the LLM to figure it out.
E. Evidence Store
Persist the investigation itself:
Investigation
 ├── request
 ├── context
 ├── entities
 ├── timeline
 ├── evidence
 ├── hypotheses
 ├── contradictions
 ├── code references
 └── conclusion
This makes investigations resumable and auditable.

3. The agent should operate on an evidence graph
This is where I think the design becomes interesting.
Instead of giving the LLM a giant collection of logs, construct an investigation evidence graph.
For example:
                Session S123
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       Request     User       Entity
          │
          ▼
       Job J456
          │
       ┌──┴────┐
       ▼       ▼
   Log E123   Log E124
       │
       ▼
   Exception X
       │
       ▼
   Service Foo
       │
       ▼
 FooService.process()
       │
       ▼
   Java source
       │
       ▼
   Code commit
Now the agent isn't merely doing semantic search.
It is traversing relationships between evidence.

4. MCP fits underneath the evidence layer
This is where I'd use MCP.
For example:
Investigation Agent
       │
       ▼
Evidence Tool Interface
       │
       ├───────────────┐
       ▼               ▼
   Elastic MCP      Code MCP
       │               │
       ▼               ▼
      ELK           Repository

       │
       ▼
   Oracle Adapter
You could also expose Oracle through MCP if there's a suitable MCP implementation, but MCP shouldn't dictate the architecture.
Your internal interface might be:
search_runtime_evidence(...)
get_application_state(...)
search_source(...)
get_source(...)
get_code_history(...)
The implementations can use MCP, JDBC, APIs, etc.
That gives you the option of:
Agent
  ↓
Tool Gateway
  ↓
MCP
  ↓
Elastic
or:
Agent
  ↓
Tool Gateway
  ↓
Oracle JDBC
without changing the agent.

5. The investigation shouldn't be a fixed workflow
This is another important distinction.
A traditional workflow might be:
Get logs
→ Get DB
→ Get code
→ Generate answer
An agentic investigation should be adaptive.
For example:
Case A
Agent gets:
Session ID
ELK immediately shows:
NullPointerException
FooService.java:241
The agent can jump directly to source.
Case B
ELK shows no obvious error.
The agent discovers:
Oracle:
state = PROCESSING
last_event = PAYMENT_STARTED
It searches ELK around that event and discovers the service stopped emitting events.
Then it investigates the relevant Java path.
Case C
The code appears correct.
The agent discovers:
Oracle state ≠ expected state
and investigates transaction boundaries / persistence behavior.
The next tool call depends on the evidence already obtained.
That's what makes it agentic.

6. Give the agent an investigation loop
Something like:
                  ┌─────────────────┐
                  │ Investigation   │
                  │ Context         │
                  └────────┬────────┘
                           ▼
                  ┌─────────────────┐
                  │ Form hypothesis │
                  └────────┬────────┘
                           ▼
                  ┌─────────────────┐
                  │ Request evidence│
                  └────────┬────────┘
                           ▼
                  ┌─────────────────┐
                  │ Evaluate        │
                  │ evidence        │
                  └────────┬────────┘
                           │
                 ┌─────────┴─────────┐
                 │                   │
             insufficient         sufficient
                 │                   │
                 ▼                   ▼
          New investigation      Verify against
             direction             source/state
                                     │
                                     ▼
                              Root cause?
                               /       \
                             No         Yes
                             │           │
                             └─────┐     ▼
                                   │  Conclude
                                   │
                                   └──► continue

7. Separate facts from hypotheses
This is critical for a root-cause agent.
The investigation state should distinguish:
{
  "facts": [
    "Session S123 entered PROCESSING at 10:31:42",
    "FooService logged timeout at 10:32:01"
  ],

  "hypotheses": [
    {
      "statement": "Timeout path fails to transition session state",
      "status": "UNDER_INVESTIGATION",
      "supporting_evidence": ["E17", "E21"],
      "contradicting_evidence": []
    }
  ]
}
Then eventually:
Hypothesis
     ↓
Evidence
     ↓
Code inspection
     ↓
Verification
     ↓
CONFIRMED
This substantially reduces the tendency of an LLM to turn a plausible explanation into a claimed root cause.

8. Source-code investigation needs its own intelligence layer
Don't just give the agent grep.
You want the code side to understand:
repository
    ↓
module
    ↓
package
    ↓
class
    ↓
method
    ↓
call graph
    ↓
exception paths
    ↓
configuration
    ↓
database interaction
Useful capabilities include:
find_symbol()
get_symbol_source()
find_callers()
find_callees()
find_exception_handlers()
find_database_operations()
search_code()
get_commit_history()
compare_versions()
The ideal investigation progression becomes:
ELK event
   ↓
service
   ↓
operation
   ↓
class/method
   ↓
source
   ↓
exception path
   ↓
state mutation

9. Application onboarding becomes the scalable part
If "most systems fit this pattern," don't customize the agent per application.
Create an Application Investigation Profile.
For example:
application:
  id: payments
  name: Payment Processing

observability:
  provider: elastic
  indices:
    - payments-application-logs
  session_field: sessionId
  timestamp_field: timestamp

state:
  provider: oracle
  session_table: APP_SESSION
  session_key: SESSION_ID

code:
  repository: payments-service
  language: java

correlation:
  fields:
    - sessionId
    - requestId
    - transactionId
    - correlationId
Another application gets another profile:
Application A
   ELK + Oracle + Java

Application B
   ELK + Oracle + Java

Application C
   ELK + PostgreSQL + Java

Application D
   ELK + Oracle + Python
Same investigation engine. Different evidence configuration.
That's how I'd make this a platform rather than a one-off agent.

10. The architecture I'd ultimately target
                       ┌─────────────────────┐
                       │   Investigation UI  │
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │ Investigation API   │
                       └──────────┬──────────┘
                                  │
                                  ▼
                ┌────────────────────────────────┐
                │     Investigation Orchestrator │
                │                                │
                │  State / lifecycle / policy    │
                └───────────────┬────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │    Investigation      │
                    │        Agent          │
                    │                       │
                    │ Planner               │
                    │ Hypothesis Manager    │
                    │ Evidence Evaluator    │
                    │ RCA Reasoner           │
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │   Evidence Gateway    │
                    └───────────┬───────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
       Observability       Application        Code
          Provider           Provider        Provider
              │                 │                 │
              ▼                 ▼                 ▼
             ELK              Oracle         Git/Repo
              ▲
              │
           Kafka
        (ingestion only)

                    ┌─────────────────────┐
                    │ Investigation Store │
                    │                     │
                    │ Evidence            │
                    │ Timeline            │
                    │ Hypotheses          │
                    │ Findings            │
                    └─────────────────────┘
The design principle I'd use
The LLM reasons about the investigation. The platform retrieves and structures evidence. The adapters know the infrastructure. The application profile knows where that application's evidence lives.
That gives you a generic RCA platform that can investigate a large number of applications without building a bespoke agent for each one.
And MCP becomes an implementation option for the Evidence Gateway, rather than becoming the architecture itself.
