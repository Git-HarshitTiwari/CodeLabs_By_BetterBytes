# CodeLabs By BetterBytes

**CodeLabs** is a real-time collaborative coding platform developed by **BetterBytes**.  
It enables multiple developers to write, edit, run, and debug code together in a shared browser-based workspace.

The platform combines **live code synchronization, AI-assisted coding, role-based collaboration, and a secure multi-language execution engine** to improve remote development workflows.

---

## Features

- **Real-Time Collaborative Coding**
  - Multiple users can edit the same code file simultaneously
  - Live synchronization across connected collaborators
  - Cursor presence and collaborative editing

- **AI Coding Assistant**
  - Integrated assistant powered by **Google Gemini 2.5 Flash**
  - Context-aware responses using current code
  - Multi-turn conversation support

- **Multi-Language Code Execution**
  Supports running code for:
  - Python
  - JavaScript
  - C++
  - Java
  - Rust

- **Role-Based Access Control**
  Four permission roles:
  - Owner
  - Developer
  - Tester
  - Client

- **Collaboration Tools**
  - Live code syncing
  - Team chat
  - Git operation notifications
  - Real-time updates via WebSockets

---

## Tech Stack

### Backend
- Python 3
- Flask
- Flask-SocketIO
- WebSockets

### AI Integration
- Google Gemini 1.5 Flash
- google-generativeai SDK

### Security
- PyJWT (JWT authentication)
- bcrypt (password hashing)
- Role-Based Access Control (RBAC)

### Storage
- Redis
- In-memory fallback storage

### Code Execution Engine
- Subprocess sandbox execution
- Compile and run pipeline for multiple languages

### Frontend
- HTML
- CSS
- JavaScript
- CodeMirror Editor

### Configuration
- python-dotenv
- Auto port detection
- Single-file deployment architecture

---

## Use Cases

- Pair Programming
- Remote Development Teams
- Technical Coding Interviews
- Collaborative Debugging
- Educational Coding Sessions

---

## Future Improvements

- GitHub repository integration
- Real-time voice and video collaboration
- AI-based bug detection and code review
- Containerized execution environments
- Project workspace management
