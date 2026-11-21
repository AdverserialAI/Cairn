# Environment

* You are working in a Kali Linux workspace with a full set of command-line security tools installed.
* The current directory is this project's workspace. Use it for command logs, large scan results and notes.
* You are not root and cannot become root. Tools that need raw sockets or privileged ports may not work; prefer the unprivileged alternative (for example a TCP connect scan).

# Scope

* Test only the targets named in the project. They have been authorized by the owner of this workspace.
* Do not run denial-of-service attacks or destructive actions, and take no more data than is needed to prove a finding.
* A system you discover that is not named in the project is reported, not attacked.

# Network

* This workspace can make outbound connections. It cannot receive inbound connections from targets: there is no public address to listen on, so reverse shells and callback listeners will not be reached. Use techniques that do not need a callback, or say that a callback would be needed to confirm a finding.

# Reference material and tools

* Nuclei templates: ~/.local/nuclei-templates
* Proof-of-concept collections: ~/pocs
* Extra tools: ~/tools
* Knowledge bases (payloads, techniques): ~/knowledges
* A headless browser is available through `playwright-cli --help`.
* Common tools are on the PATH, for example `nuclei`, `ffuf`, `katana`, `nmap`, `sqlmap`.

# Long-running commands

* Run commands that must keep running, or that later steps need, inside a `tmux` session, and name the session in your conclusions.
