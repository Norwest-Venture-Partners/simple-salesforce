"""Simple-Salesforce Package"""
# flake8: noqa

from .api import AsyncSalesforce, SFType, create_salesforce_client
from .bulk import SFBulkHandler
from .exceptions import (SalesforceAuthenticationFailed, SalesforceError,
                         SalesforceExpiredSession, SalesforceGeneralError,
                         SalesforceMalformedRequest,
                         SalesforceMoreThanOneRecord, SalesforceRefusedRequest,
                         SalesforceResourceNotFound)
from .login import AsyncSalesforceLogin
from .format import format_soql, format_external_id
