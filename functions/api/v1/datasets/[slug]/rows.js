import { answer } from '../../../../_api.js';

// Rows of the newest version, filtered and paged.
export const onRequestGet = (context) => answer(context, 'rows');
