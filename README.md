# Billpal skills

Free skills for Claude and ChatGPT. Each skill runs on its own. A Billpal account makes some of them easier, because the assistant can read invoices and contacts the user already has. Without an account, the user supplies the same details in the chat.

- [Invoice generator](invoice-generator/): writes a normal invoice in a Billpal template, your own file, or a plain default. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/invoice-generator.zip)
- [SEPA file generator](sepa-file-generator/): builds one SEPA credit-transfer file (pain.001.001.09) for upload to business online banking. Needs code execution turned on. [Download ZIP](https://github.com/leebale/billpal-skills/releases/latest/download/sepa-file-generator.zip)

To use a skill:

- **Claude:** upload the ZIP under Settings, Capabilities, Skills. Turn on code execution.
- **ChatGPT:** drop the ZIP into the chat and ask it to follow the skill. A link alone is not enough for the SEPA skill, because ChatGPT cannot download the files into its sandbox.

Billpal: https://billpal.io
