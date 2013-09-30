"""A notebook manager that uses the KBase workspace for storage.

Based on Travis Harrison's shocknbmanager and the azurenbmanager

Authors:

* Steve Chan <sychan@lbl.gov>

Copyright (C) 2013 The Regents of the University of California
Department of  Energy contract-operators of the Lawrence Berkeley National Laboratory
1 Cyclotron Road, Berkeley,  CA 94720 

Copyright (C) 2013  The KBase Project

Distributed unspecified open source license as of 9/27/2013  

"""

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------

import datetime
import dateutil.parser
import io
import os
import glob
import shutil
import json
import re
import biokbase.narrative.ws_util as ws_util
from biokbase.workspaceService.Client import workspaceService

from bson.json_util import dumps

from unicodedata import normalize

from tornado import web

from IPython.html.services.notebooks.nbmanager import NotebookManager
from IPython.config.configurable import LoggingConfigurable
from IPython.nbformat import current
from IPython.utils.traitlets import Unicode, Dict, Bool, List, TraitError
from IPython.utils import tz

#-----------------------------------------------------------------------------
# Classes
#-----------------------------------------------------------------------------

class KBaseWSNotebookManager(NotebookManager):
    """
    A notebook manager that uses the KBase workspace for storage.

    The Workspace backend simply adds a few metadata fields into the
    notebook object and pushes it into the workspace as the 'data'
    part of a workspace object

    Additional metadata fields
    {
        'id' : User specified title for the narrative alphanumerica + _
        'creator' : {username of the creator of this notebook},
        'description' : 'description of notebook',
        'data_dependencies' : { list of kbase id strings }
        'format' : self.node_format
    }

    This handler expects that on every request, the session attribute for an
    instance will be populated by the front end handlers. That's gross, but
    that's what we're running with for now.
    Note: you'll probably see "That's gross, but..." a lot in this rev of the
    code

    Notebooks are identified with workspace identifiers of the format
    {workspace_name}.{object_name}
    """
    kbasews_uri = Unicode('https://kbase.us/services/workspace/', config=True, help='Workspace service endpoint URI')

    ipynb_type = Unicode(u'ipynb')
    allowed_formats = List([u'json'])
    node_format = ipynb_type
    ws_type = Unicode('Narrative', config=True, help='Type to use within workspace service')
    # regex for parsing out workspace_id and object_id from
    # a "kb|ws.{workspace}.{object}" string
    ws_regex = re.compile( '^(?P<wsid>\w+)\.(?P<objid>\w+)')

    # This is a regular expression to make sure that the workspace ID doesn't contain
    # non-legit characters in the object ID field
    # We use it like this to to translate names:
    # wsid_regex.sub('',"Hello! Freaking World! 123".replace(' ','_'))
    # to get an id of 'Hello_Freaking_World_123'
    # We will enforce validation on the narrative naming GUI, but this is a safety net
    wsid_regex = re.compile('[\W]+', re.UNICODE)    

    def __init__(self, **kwargs):
        """Verify that we can connext to the configured WS instance"""
        super( NotebookManager, self).__init__(**kwargs)
        if not self.kbasews_uri:
            raise web.HTTPError(412, u"Missing KBase workspace service endpoint URI.")

        self.wsclient = workspaceService( self.kbasews_uri)
        # Verify that we can fetch list of types back to make sure the configured uri is good
        try:
            self.all_types = self.wsclient.get_types()
        except Exception as e:
            raise web.HTTPError( 500, u"Unable to connect to workspace service at %s: %s " % (self.kbasews_uri, e))
        mapping = Dict()
        # Map notebook names to notebook_ids
        rev_mapping = Dict()
        # Setup empty hash for session object
        self.kbase_session = {}

    def _clean_id( self,id):
        return self.wsid_regex.sub( '', id.replace(' ','_'))
            
    def list_notebooks(self):
        """List all notebooks in WSS
        For the ID field, we use "{ws_id}.{obj_id}"
        The obj_id field is sanitized version of document.ipynb.metadata.name
        """
        self.log.debug("listing notebooks.")
        self.log.debug("kbase_session = %s" % str(self.kbase_session))
        try:
            user_id = self.kbase_session['user_id']
        except KeyError:
            self.log.debug("No user_id in session")
            return []
        try:
            token = self.kbase_session['token']
        except KeyError:
            self.log.debug("No token in session")
            return []
        # Grab all workspaces, filter it down to the ones the user have privs on
        # and then extract all the Narrative objects from each one
        all = ws_util.get_wsobj_meta( self.wsclient, token, ws="%s_home" % user_id)

        self.mapping = {
            "%s.%s" % (all[ws_id]['workspace'],all[ws_id]['id']) : "%s.%s" % (all[ws_id]['workspace'],all[ws_id]['id'])
            for ws_id in all.keys()
        }
        self.rev_mapping = self.mapping
        data = [ dict(notebook_id = it[0], name = it[1]) for it in self.mapping.items()]
        data = sorted(data, key=lambda item: item['name'])
        return data

    def new_notebook_id(self, name):
        """Generate a new notebook_id for a name and store its mappings."""
        notebook_id = "%s_home.%s" % ( self.kbase_session['user_id'],name)
        self.mapping[notebook_id] = name
        self.rev_mapping[name] = notebook_id
        return notebook_id

    def delete_notebook_id(self, notebook_id):
        """Delete a notebook's id in the mapping."""
        self.log.debug("delete_notebook_id(%s)"%(notebook_id))
        try:
            user_id = self.kbase_session['user_id']
        except KeyError:
            raise web.HTTPError(400, u'Missing user_id from kbase_session object')
        try:
            token = self.kbase_session['token']
        except KeyError:
            raise web.HTTPError(400, u'Missing token from kbase_session object')
        name = self.mapping[notebook_id]
        super(KBaseWSNotebookManager, self).delete_notebook_id(notebook_id)

    def notebook_exists(self, notebook_id):
        """Does a notebook exist?"""
        exists = super(KBaseWSNotebookManager, self).notebook_exists(notebook_id)
        self.log.debug("notebook_exists(%s) = %s"%(notebook_id,exists))
        if not exists:
            return False
        return exists
    
    def get_name(self, notebook_id):
        """get a notebook name, raising 404 if not found"""
        self.log.debug("get_name(%s) = %s"%(notebook_id))
        try:
            name = self.mapping[notebook_id]
        except KeyError:
            raise web.HTTPError(404, u'Notebook does not exist: %s' % notebook_id)
        return name

    def read_notebook_object(self, notebook_id):
        """Get the Notebook representation of a notebook by notebook_id."""
        self.log.debug("reading notebook %s." % notebook_id)
        try:
            user_id = self.kbase_session['user_id']
        except KeyError:
            raise web.HTTPError(400, u'Missing user_id from kbase_session object')
        try:
            token = self.kbase_session['token']
        except KeyError:
            raise web.HTTPError(400, u'Missing token from kbase_session object')
        try:
            wsobj = ws_util.get_wsobj( self.wsclient, token, "kb|ws.%s" % notebook_id, self.ws_type)
        except ws_util.BadWorkspaceID, e:
            raise web.HTTPError(500, u'Notebook % not found: %' % (notebook_id, e))
        jsonnb = json.dumps(wsobj['data'])
        self.log.debug("jsonnb = %s" % jsonnb)
        nb = current.reads(jsonnb,u'json')
        last_modified = dateutil.parser.parse(wsobj['metadata']['moddate'])
        return last_modified, nb
    
    def write_notebook_object(self, nb, notebook_id=None):
        """Save an existing notebook object by notebook_id."""
        self.log.debug("writing notebook %s." % notebook_id)
        try:
            user_id = self.kbase_session['user_id']
        except KeyError:
            raise web.HTTPError(400, u'Missing user_id from kbase_session object')
        try:
            token = self.kbase_session['token']
        except KeyError:
            raise web.HTTPError(400, u'Missing token from kbase_session object')
        try:
            new_name = normalize('NFC', nb.metadata.name)
        except AttributeError:
            raise web.HTTPError(400, u'Missing notebook name')
        new_name = self._clean_id( new_name)
        # Carry over some of the metadata stuff from ShockNBManager
        try:
            if notebook_id is None:
                notebook_id = self.new_notebook_id(new_name)
            if not hasattr(nb.metadata, 'creator'):
                nb.metadata.creator = user_id
            #if not hasattr(nb.metadata, 'id'):
            #    m = self.ws_regex.match(notebook_id)
            #    nb.metadata.id = m.group('objid')
            if not hasattr(nb.metadata, 'type'):
                nb.metadata.type = 'Narrative'
            if not hasattr(nb.metadata, 'description'):
                nb.metadata.description = ''
            if not hasattr(nb.metadata, 'data_dependencies'):
                nb.metadata.data_dependencies = []
            nb.metadata.format = self.node_format
        except Exception as e:
            raise web.HTTPError(400, u'Unexpected error setting notebook attributes: %s' %e)
        if notebook_id not in self.mapping:
            raise web.HTTPError(404, u'Notebook does not exist: %s' % notebook_id)
        try:
            wsobj = { 'id' : self._clean_id(nb.metadata.name),
                      'type' : self.ws_type,
                      'data' : nb,
                      'workspace' : 'kbasetest_home',
                      'command' : '',
                      'metadata' : nb.metadata,
                      'auth' : token,
                      'json' : 0,
                      'compressed': 0,
                      'retrieveFromURL': 0,
                      'asHash' :  0
                    }
            res = self.wsclient.save_object( wsobj)
            self.log.debug("save_object returned %s" % res)
        except Exception as e:
            raise web.HTTPError(500, u'%s saving notebook: %s' % (type(e),e))
        # use "ws_id.object_id" as the identifier
        id = "%s.%s" % ( res[7], res[0])
        self.mapping[id] = new_name
        return id

    def delete_notebook(self, notebook_id):
        """Delete notebook by notebook_id."""
        self.log.debug("deleting notebook %s" % notebook_id)
        try:
            user_id = self.kbase_session['user_id']
        except KeyError:
            raise web.HTTPError(400, u'Missing user_id from kbase_session object')
        try:
            token = self.kbase_session['token']
        except KeyError:
            raise web.HTTPError(400, u'Missing token from kbase_session object')
        if notebook_id is None:
            raise web.HTTPError(400, u'Missing notebookd_id')
        self.log.debug("deleting notebook %s", notebook_id)
        m = self.ws_regex.match(notebook_id)
        if m:
            param = { 'auth' : token,
                  'type' : self.ws_type,
                  'workspace' : m.group('wsid'),
                  'id' : m.group('objid')
            }
            res = self.wsclient.delete_object( param)
            self.log.debug("delete object result: %s" % res)

        else:
            raise ws_util.BadWorkspaceID( noteboot_id)
        self.delete_notebook_id(notebook_id)

    # public checkpoint API
    # Checkpoints in the MongoDB manager are just another field in the
    # overall MongoDB document. We copy the ipynb field into the ipynb_chkpt
    # field (and vice versa for revert)
    def create_checkpoint(self, notebook_id):
        """Create a checkpoint from the current state of a notebook"""
        # only the one checkpoint ID:
        checkpoint_id = u"checkpoint"
        chkpt_created = datetime.datetime.utcnow()
        # This is a no-op for now
        # return the checkpoint info
        return { 'checkpoint_id' : checkpoint_id , 'last_modified' : chkpt_created}


    def list_checkpoints(self, notebook_id):
        """
        list the checkpoints for a given notebook
        this is a no-op for now. eventually use it for rolling back to old revs in ws
        """
        return []
    
    def restore_checkpoint(self, notebook_id, checkpoint_id):
        """restore a notebook to a checkpointed state"""
        pass

    def delete_checkpoint(self, notebook_id, checkpoint_id):
        """delete a notebook's checkpoint"""
        pass

    def log_info(self):
        #self.log.info("Serving notebooks from MongoDB URI %s" %self.mongodb_uri)
        #self.log.info("Serving notebooks from MongoDB db %s" %self.mongodb_database)
        #self.log.info("Serving notebooks from MongoDB collection %s" %self.mongodb_collection)
        pass

    def info_string(self):
        return "Workspace Notebook Service with workspace endpoint at %s" % self.kbasews_uri
