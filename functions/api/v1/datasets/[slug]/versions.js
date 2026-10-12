import { versions } from '../../../../_api.js';

// Every version the query API answers, newest first.
export const onRequestGet = (context) => versions(context);
