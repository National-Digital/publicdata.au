import { answer } from '../../../../_api.js';

// Counts, sums, averages, minimums and maximums by group over the newest version.
export const onRequestGet = (context) => answer(context, 'aggregate');
