import { json } from '../_lib.js';

// Any path under /api/ that no endpoint answers: a JSON 404 that says where the API is.
export const onRequest = () =>
  json(
    {
      error: 'No such API path',
      docs: 'https://publicdata.au/agents/#query-api',
      openapi: 'https://publicdata.au/openapi.json',
    },
    404,
  );
