import { answer } from '../../../../../../_api.js';

// Rows of one dated version. The answer never changes, so it is cached for good.
export const onRequestGet = (context) => answer(context, 'rows');
