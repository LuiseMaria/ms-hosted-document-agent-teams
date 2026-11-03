# Optional API Design Drafts

These documents describe possible REST and GraphQL integrations. They are
design drafts, not implemented services or deployment inputs. The current
hosted agent and Teams integration do not depend on them.

- [REST / OpenAPI draft](document-oas3.yml): asynchronous generation jobs,
  status queries and feedback.
- [GraphQL draft](document-schema.gql): an alternative schema for the same domain.

The API hostnames are examples. These documents do not deploy endpoints or
establish support for the operations and document types they describe.

## Protocol Comparison

| Kriterium           | SOAP                                                           | REST                                                   | GraphQL                                                                     |
|---------------------|----------------------------------------------------------------|--------------------------------------------------------|-----------------------------------------------------------------------------|
| **Vertragsmodell**  | Sehr strikt über WSDL/XSD                                      | Präzise über OpenAPI/JSON Schema                       | Stark typisiert, Enums und Pflichtfelder direkt im Schema                   |
| **Asynchrone Jobs** | Möglich, aber technisch schwergewichtig                        | Natürlich über POST, `202 Accepted` und Job-Ressource  | Möglich über Mutations, Queries und optional Subscriptions                  |
| **Abfragen**        | Meist feste Operationen                                        | Feste, cachebare Ressourcen                            | Client bestimmt exakt die benötigten Felder                                 |
| **Komplexität**     | Hoch                                                           | Niedrig bis mittel                                     | Mittel bis hoch: Resolver, Query-Limits, N+1-Schutz                         |
| **HTTP-Semantik**   | Wird überwiegend nur als Transport genutzt                     | Nutzt Statuscodes, Methoden, Header und Caching direkt | Meist ein Endpoint; fachliche Fehler häufig trotz HTTP 200                  |
| **Tooling/Betrieb** | Etabliert in klassischen Enterprise-Landschaften               | Sehr breit unterstützt und einfach zu überwachen       | Gutes Schema-Tooling, aber anspruchsvollere Autorisierung und Observability |
| **Geeignet für**    | Legacy-Integration, WS-Security und verbindliche SOAP-Vorgaben | Klar abgegrenzte Services und Workflows                | Flexible, verknüpfte Datenmodelle mit unterschiedlichen Client-Abfragen     |
