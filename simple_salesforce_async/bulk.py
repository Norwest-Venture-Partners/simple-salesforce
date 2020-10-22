""" Classes for interacting with Salesforce Bulk API """

import json
import aiohttp
import concurrent.futures

from collections import OrderedDict
from time import sleep
from functools import partial
from .util import async_call_salesforce


class SFBulkHandler:
    """ Bulk API request handler
    Intermediate class which allows us to use commands,
     such as 'sf.bulk.Contacts.create(...)'
    This is really just a middle layer, whose sole purpose is
    to allow the above syntax
    """

    def __init__(self, session_id, bulk_url, session=None):
        """Initialize the instance with the given parameters.

        Arguments:

        * session_id -- the session ID for authenticating to Salesforce
        * bulk_url -- API endpoint set in Salesforce instance
        * session -- Custom requests session, created in calling code. This
                     enables the use of requests Session features not otherwise
                     exposed by simple_salesforce_async.
        """
        self.session_id = session_id
        self.session = session or aiohttp.ClientSession()
        self.bulk_url = bulk_url

        # Define these headers separate from Salesforce class,
        # as bulk uses a slightly different format
        self.headers = {
            'Content-Type': 'application/json',
            'X-SFDC-Session': self.session_id,
            'X-PrettyPrint': '1'
            }

    def __getattr__(self, name):
        return SFBulkType(
            object_name=name,
            bulk_url=self.bulk_url,
            headers=self.headers,
            session=self.session
        )


class SFBulkType:
    """ Interface to Bulk/Async API functions"""

    def __init__(self, object_name, bulk_url, headers, session):
        """Initialize the instance with the given parameters.

        Arguments:

        * object_name -- the name of the type of SObject this represents,
                         e.g. `Lead` or `Contact`
        * bulk_url -- API endpoint set in Salesforce instance
        * headers -- bulk API headers
        * session -- Custom requests session, created in calling code. This
                     enables the use of requests Session features not otherwise
                     exposed by simple_salesforce_async.
        """
        self.object_name = object_name
        self.bulk_url = bulk_url
        self.session = session
        self.headers = headers

    async def _create_job(self, operation, object_name, use_serial, external_id_field=None):
        """ Create a bulk job

        Arguments:

        * operation -- Bulk operation to be performed by job
        * object_name -- SF object
        * use_serial -- Process batches in order
        * external_id_field -- unique identifier field for upsert operations
        """

        if use_serial:
            use_serial = 1
        else:
            use_serial = 0
        payload = {
            'operation': operation,
            'object': object_name,
            'concurrencyMode': use_serial,
            'contentType': 'JSON'
            }

        if operation == 'upsert':
            payload['externalIdFieldName'] = external_id_field

        url = "{}{}".format(self.bulk_url, 'job')

        result = await async_call_salesforce(
            url=url,
            method='POST',
            session=self.session,
            headers=self.headers,
            data=json.dumps(payload)
        )
        return await result.json()

    async def _close_job(self, job_id):
        """ Close a bulk job """
        payload = {
            'state': 'Closed'
            }

        url = "{}{}{}".format(self.bulk_url, 'job/', job_id)

        result = await async_call_salesforce(
            url=url,
            method='POST',
            session=self.session,
            headers=self.headers,
            data=json.dumps(payload)
        )
        return await result.json()

    async def _get_job(self, job_id):
        """ Get an existing job to check the status """
        url = "{}{}{}".format(self.bulk_url, 'job/', job_id)

        result = await async_call_salesforce(
            url=url,
            method='GET',
            session=self.session,
            headers=self.headers
        )
        return await result.json()

    async def _add_batch(self, job_id, data, operation):
        """ Add a set of data as a batch to an existing job
        Separating this out in case of later
        implementations involving multiple batches
        """

        url = "{}{}{}{}".format(self.bulk_url, 'job/', job_id, '/batch')

        if operation != 'query':
            data = json.dumps(data)

        result = await async_call_salesforce(
            url=url,
            method='POST',
            session=self.session,
            headers=self.headers, data=data
        )
        return await result.json()

    async def _get_batch(self, job_id, batch_id):
        """ Get an existing batch to check the status """

        url = "{}{}{}{}{}".format(self.bulk_url, 'job/',
                                  job_id, '/batch/', batch_id)

        result = await async_call_salesforce(
            url=url,
            method='GET',
            session=self.session,
            headers=self.headers
        )
        return await result.json()

    async def _get_batch_results(self, job_id, batch_id, operation):
        """ retrieve a set of results from a completed job """

        url = "{}{}{}{}{}{}".format(self.bulk_url, 'job/', job_id, '/batch/',
                                    batch_id, '/result')

        result = await async_call_salesforce(
            url=url,
            method='GET',
            session=self.session,
            headers=self.headers
        )

        if operation == 'query':
            url_query_results = "{}{}{}".format(url, '/', result.json()[0])
            query_result = await async_call_salesforce(
                url=url_query_results,
                method='GET',
                session=self.session,
                headers=self.headers
            )
            return query_result.json()

        return result.json()

    # pylint: disable=R0913
    async def worker(self, batch, operation, wait=5):
        """ Gets batches from concurrent worker threads.
        self._bulk_operation passes batch jobs.
        The worker function checks each batch job waiting for it complete
        and appends the results.
        """

        batch_status = await self._get_batch(job_id=batch['jobId'], batch_id=batch['id'])['state']

        while batch_status not in ['Completed', 'Failed', 'Not Processed']:
            sleep(wait)
            batch_status = await self._get_batch(
                job_id=batch['jobId'],
                batch_id=batch['id']
            )['state']

        batch_results = await self._get_batch_results(
            job_id=batch['jobId'],
            batch_id=batch['id'],
            operation=operation
        )

        result = batch_results
        return result

    async def _bulk_operation(self, object_name, operation, data, use_serial=False,
                              external_id_field=None, batch_size=10000, wait=5):
        """ String together helper functions to create a complete
        end-to-end bulk API request
        Arguments:
        * object_name -- SF object
        * operation -- Bulk operation to be performed by job
        * data -- list of dict to be passed as a batch
        * use_serial -- Process batches in serial mode
        * external_id_field -- unique identifier field for upsert operations
        * wait -- seconds to sleep between checking batch status
        * batch_size -- number of records to assign for each batch in the job
        """

        if operation != 'query':
            # Checks to prevent batch limit
            if len(data) >= 10000 and batch_size > 10000:
                batch_size = 10000
            pool = concurrent.futures.ThreadPoolExecutor()

            job = await self._create_job(
                object_name=object_name,
                operation=operation,
                use_serial=use_serial,
                external_id_field=external_id_field
            )

            batches = [
                await self._add_batch(job_id=job['id'], data=i, operation=operation)
                for i in
                [data[i * batch_size:(i + 1) * batch_size]
                 for i in range((len(data) // batch_size + 1))] if i]

            multi_thread_worker = partial(self.worker, operation=operation)
            list_of_results = pool.map(multi_thread_worker, batches)

            results = [i for sublist in list_of_results for i in sublist]

            await self._close_job(job_id=job['id'])

        if operation == 'query':
            job = await self._create_job(
                object_name=object_name,
                operation=operation,
                use_serial=use_serial,
                external_id_field=external_id_field
            )

            batch = await self._add_batch(job_id=job['id'], data=data, operation=operation)

            await self._close_job(job_id=job['id'])

            batch_status = self._get_batch(job_id=batch['jobId'], batch_id=batch['id'])['state']

            while batch_status not in ['Completed', 'Failed', 'Not Processed']:
                sleep(wait)
                batch_status = await self._get_batch(job_id=batch['jobId'], batch_id=batch['id'])['state']

            results = await self._get_batch_results(job_id=batch['jobId'], batch_id=batch['id'], operation=operation)

        return results

    # _bulk_operation wrappers to expose supported Salesforce bulk operations
    async def delete(self, data, batch_size=10000, use_serial=False):
        """ soft delete records """
        results = await self._bulk_operation(
            object_name=self.object_name,
            use_serial=use_serial,
            operation='delete',
            data=data,
            batch_size=batch_size
        )
        return results

    async def insert(self, data, batch_size=10000, use_serial=False):
        """ insert records """
        results = await self._bulk_operation(
            object_name=self.object_name,
            use_serial=use_serial,
            operation='insert',
            data=data,
            batch_size=batch_size
        )
        return results

    async def upsert(self, data, external_id_field, batch_size=10000, use_serial=False):
        """ upsert records based on a unique identifier """
        results = await self._bulk_operation(
            object_name=self.object_name,
            use_serial=use_serial,
            operation='upsert',
            external_id_field=external_id_field,
            data=data,
            batch_size=batch_size
        )
        return results

    async def update(self, data, batch_size=10000, use_serial=False):
        """ update records """
        results = await self._bulk_operation(
            object_name=self.object_name,
            use_serial=use_serial,
            operation='update',
            data=data,
            batch_size=batch_size
        )
        return results

    async def hard_delete(self, data, batch_size=10000, use_serial=False):
        """ hard delete records """
        results = await self._bulk_operation(
            object_name=self.object_name,
            use_serial=use_serial,
            operation='hardDelete',
            data=data,
            batch_size=batch_size
        )
        return results

    async def query(self, data):
        """ bulk query """
        results = await self._bulk_operation(object_name=self.object_name, operation='query', data=data)
        return results
