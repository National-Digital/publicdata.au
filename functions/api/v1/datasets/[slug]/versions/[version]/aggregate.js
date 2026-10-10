import { answer } from '../../../../../../_api.js';

// Aggregates over one dated version, cached for good.
export const onRequestGet = (context) => answer(context, 'aggregate');
