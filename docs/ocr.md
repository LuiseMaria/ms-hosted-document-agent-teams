| Criterion                | Tesseract                 | Document Intelligence       | Content Understanding                                 |
|--------------------------|---------------------------|-----------------------------|-------------------------------------------------------|
| License costs            | 0 EUR                     | Pay per page                | Pay per page + GenAI                                  |
| Operating costs          | Compute only              | Managed service             | Managed service                                       |
| OCR quality for scans    | Medium                    | High                        | High                                                  |
| Table extraction         | Weak                      | Very good                   | Very good                                             |
| Key-value extraction     | No                        | Yes                         | Yes                                                   |
| Structured JSON output   | No                        | Yes                         | Yes                                                   |
| Operations / Maintenance | Self-managed              | Microsoft-managed           | Microsoft-managed                                     |
| Private environment      | Easy                      | Private Endpoint possible   | Private Endpoint possible                             |
| PDFs with poor quality   | Often problematic         | Significantly more robust   | Significantly more robust                             |
| Deployment effort        | High: Azure Container App | Medium: Azure Resource      | Low: Foundry included                                 |
| Location                 | any, no constraints       | not in germany-west-central | not in gwc, foundry must be in region where available |


## Tesseract
Including tesseract in hosted agent makes less sense because it will couple scaling of agent with OCR processing, which is not ideal for performance and cost.
Possible workflow would be:
- ACA Job: pre-processing batch run of documents with tesseract in a separate container app, storing results in blob storage
  - all documents will be processed, even if not needed, which is not cost effective
  - additional orchestration is needed to manage the batch run and results
or
- ACA MCP: providing tesseract as mcp tool and agent decides to call it or not, depending on the document type and quality