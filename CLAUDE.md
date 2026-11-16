# Dev ports (fixed for this project)

- Frontend (Vite): **http://localhost:7070** -- `vite.config.ts` pins it with `strictPort`. Port 5173 on this machine belongs to a different app; never use it for this project.
- Backend (Express): **3001** -- run `$env:PORT="3001"; npm run dev:server`.
- Run the frontend with `$env:BACKEND_PORT="3001"; npm run dev:client` so the Vite proxy (`/api`, `/data`) reaches the backend.
- Restart a process by exact PID (`netstat -ano` + `taskkill`), not PowerShell Stop-Process.
