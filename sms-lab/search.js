'use strict';

const normalize = (value) => String(value || '')
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}_]+/gu, '');

const matches = (service, query) => {
    const normalizedQuery = normalize(query);
    if (!normalizedQuery) return true;
    const aliases = Array.isArray(service?.aliases) ? service.aliases : [];
    return [service?.code, service?.name, ...aliases]
        .some((value) => normalize(value).includes(normalizedQuery));
};

const SMSServiceSearch = { normalize, matches };

if (typeof module !== 'undefined' && module.exports) module.exports = SMSServiceSearch;
if (typeof window !== 'undefined') window.SMSServiceSearch = SMSServiceSearch;
