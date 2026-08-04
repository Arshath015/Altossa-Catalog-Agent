/**
 * index.ts
 * ---------
 * Server entry point. This is the file you run to start the backend.
 *
 * Run with:  npx tsx App/server/index.ts   (from the project root)
 * (see the "Setup / how to run" notes wherever you got this file)
 */
import 'dotenv/config';
import express from 'express';
import path from 'path';
import catalogChatRouter from './catalogChatRoute';

const app = express();
const PORT = process.env.PORT ? Number(process.env.PORT) : 3000;

app.use(express.json());

// Serve the extracted brand data (page images, mini-PDFs) so URLs like
// /data/Bolzan/images/awase_p17-017.jpg resolve in the browser/chat widget.
// data/ lives at the project root (sibling of App/), so two levels up from here.
app.use('/data', express.static(path.join(__dirname, '../../data')));

// Catalog chat API -> POST /api/catalog/chat, POST /api/catalog/reload/:brand
app.use('/api/catalog', catalogChatRouter);

app.get('/', (_req: express.Request, res: express.Response) => {
  res.send('Altossa AI Catalog Agent server is running.');
});

app.listen(PORT, () => {
  console.log(`Server listening on http://localhost:${PORT}`);
  console.log(`Try: POST http://localhost:${PORT}/api/catalog/chat`);
});