"""Base index classes."""
import json
import logging
from abc import abstractmethod
from typing import Any, Dict, Generic, List, Optional, Sequence, Type, TypeVar, Union

from gpt_index.data_structs.data_structs_v2 import V2IndexStruct
from gpt_index.data_structs.node_v2 import Node
from gpt_index.docstore import DocumentStore
from gpt_index.embeddings.base import BaseEmbedding
from gpt_index.embeddings.openai import OpenAIEmbedding
from gpt_index.indices.prompt_helper import PromptHelper
from gpt_index.indices.query.base import BaseGPTIndexQuery
from gpt_index.indices.query.query_runner import QueryRunner
from gpt_index.indices.query.query_transform.base import BaseQueryTransform
from gpt_index.indices.query.schema import QueryBundle, QueryConfig, QueryMode
from gpt_index.langchain_helpers.chain_wrapper import LLMPredictor
from gpt_index.logger import LlamaLogger
from gpt_index.node_parser.interface import NodeParser
from gpt_index.node_parser.simple import SimpleNodeParser
from gpt_index.readers.schema.base import Document
from gpt_index.response.schema import Response
from gpt_index.token_counter.token_counter import llm_token_counter

IS = TypeVar("IS", bound=V2IndexStruct)

logger = logging.getLogger(__name__)


# map from mode to query class
QueryMap = Dict[str, Type[BaseGPTIndexQuery]]


class BaseGPTIndex(Generic[IS]):

    """Base LlamaIndex.

    Args:
        documents (Optional[Sequence[BaseDocument]]): List of documents to
            build the index from.
        llm_predictor (LLMPredictor): Optional LLMPredictor object. If not provided,
            will use the default LLMPredictor (text-davinci-003)
        prompt_helper (PromptHelper): Optional PromptHelper object. If not provided,
            will use the default PromptHelper.
        chunk_size_limit (Optional[int]): Optional chunk size limit. If not provided,
            will use the default chunk size limit (4096 max input size).
        include_extra_info (bool): Optional bool. If True, extra info (i.e. metadata)
            of each Document will be prepended to its text to help with queries.
            Default is True.

    """

    index_struct_cls: Type[IS]

    def __init__(
        self,
        nodes: Optional[Sequence[Node]] = None,
        index_struct: Optional[IS] = None,
        llm_predictor: Optional[LLMPredictor] = None,
        embed_model: Optional[BaseEmbedding] = None,
        docstore: Optional[DocumentStore] = None,
        prompt_helper: Optional[PromptHelper] = None,
        node_parser: Optional[NodeParser] = None,
        chunk_size_limit: Optional[int] = None,
        include_extra_info: bool = True,
        llama_logger: Optional[LlamaLogger] = None,
    ) -> None:
        """Initialize with parameters."""
        if index_struct is None and nodes is None:
            raise ValueError("One of documents or index_struct must be provided.")
        if index_struct is not None and nodes is not None:
            raise ValueError("Only one of documents or index_struct can be provided.")

        self._llm_predictor = llm_predictor or LLMPredictor()
        # NOTE: the embed_model isn't used in all indices
        self._embed_model = embed_model or OpenAIEmbedding()
        self._include_extra_info = include_extra_info

        # TODO: move out of base if we need custom params per index
        self._prompt_helper = prompt_helper or PromptHelper.from_llm_predictor(
            self._llm_predictor, chunk_size_limit=chunk_size_limit
        )

        self._docstore = docstore or DocumentStore()
        self._llama_logger = llama_logger or LlamaLogger()
        self._node_parser = node_parser or SimpleNodeParser()

        if index_struct is None:
            assert nodes is not None
            self._docstore.add_documents(nodes)
            index_struct = self.build_index_from_nodes(nodes)
            if not isinstance(index_struct, self.index_struct_cls):
                raise ValueError(
                    f"index_struct must be of type {self.index_struct_cls} "
                    "but got {type(index_struct)}"
                )

        self._index_struct = index_struct
        # update docstore with index_struct
        self._update_docstore()

    @classmethod
    def from_documents(
        cls,
        documents: Sequence[Document],
        docstore: Optional[DocumentStore] = None,
        node_parser: Optional[NodeParser] = None,
        **kwargs: Any,
    ) -> "BaseGPTIndex":
        """Create index from documents."""
        node_parser = node_parser or SimpleNodeParser()
        docstore = docstore or DocumentStore()

        for doc in documents:
            docstore.set_document_hash(doc.get_doc_id(), doc.get_doc_hash())

        nodes = node_parser.get_nodes_from_documents(documents)

        return cls(
            nodes=nodes,
            docstore=docstore,
            node_parser=node_parser,
            **kwargs,
        )

    @classmethod
    def from_indices(
        cls,
        indices: Sequence["BaseGPTIndex"],
        **kwargs: Any,
    ) -> "BaseGPTIndex":
        """Create index from other indices."""
        raise NotImplementedError()

    @property
    def prompt_helper(self) -> PromptHelper:
        """Get the prompt helper corresponding to the index."""
        return self._prompt_helper

    @property
    def docstore(self) -> DocumentStore:
        """Get the docstore corresponding to the index."""
        return self._docstore

    @property
    def llm_predictor(self) -> LLMPredictor:
        """Get the llm predictor."""
        return self._llm_predictor

    @property
    def embed_model(self) -> BaseEmbedding:
        """Get the llm predictor."""
        return self._embed_model

    def _update_docstore(self) -> None:
        """Update index registry and docstore."""
        # update docstore with current struct
        # NOTE: we call allow_update=True: in old versions of the docstore,
        # the index_struct was not stored in the docstore. whereas
        # in the new docstore, index_struct is stored in the docstore.
        # if we want to break BW compatibility, we can just remove this line
        # and only insert into docstore during index construction.
        self._docstore.add_documents([self.index_struct], allow_update=True)

    @property
    def index_struct(self) -> IS:
        """Get the index struct."""
        return self._index_struct

    @property
    def index_struct_with_text(self) -> IS:
        """Get the index struct with text.

        If text not set, raise an error.
        For use when composing indices with other indices.

        """
        # make sure that we generate text for index struct
        if self._index_struct.text is None:
            # NOTE: set text to be empty string for now
            raise ValueError(
                "Index must have text property set in order "
                "to be composed with other indices. "
                "In order to set text, please run `index.set_text()`."
            )
        return self._index_struct

    def set_text(self, text: str) -> None:
        """Set summary text for index struct.

        This allows index_struct_with_text to be used to compose indices
        with other indices.

        """
        self._index_struct.text = text

    def set_extra_info(self, extra_info: Dict[str, Any]) -> None:
        """Set extra info (metadata) for index struct.

        If this index is used as a subindex for a parent index, the metadata
        will be propagated to all nodes derived from this subindex, in the
        parent index.

        """
        self._index_struct.extra_info = extra_info

    def set_doc_id(self, doc_id: str) -> None:
        """Set doc_id for index struct.

        This is used to uniquely identify the index struct in the docstore.
        If you wish to delete the index struct, you can use this doc_id.

        """
        old_doc_id = self._index_struct.get_doc_id()
        self._index_struct.doc_id = doc_id
        # Note: we also need to delete old doc_id, and update docstore
        self._docstore.delete_document(old_doc_id)
        self._docstore.add_documents([self._index_struct], allow_update=True)

    def get_doc_id(self) -> str:
        """Get doc_id for index struct.

        If doc_id not set, raise an error.

        """
        if self._index_struct.doc_id is None:
            raise ValueError("Index must have doc_id property set.")
        return self._index_struct.doc_id

    @abstractmethod
    def _build_index_from_nodes(self, nodes: Sequence[Node]) -> IS:
        """Build the index from nodes."""

    @llm_token_counter("build_index_from_nodes")
    def build_index_from_nodes(self, nodes: Sequence[Node]) -> IS:
        """Build the index from nodes."""
        return self._build_index_from_nodes(nodes)

    @abstractmethod
    def _insert(self, nodes: Sequence[Node], **insert_kwargs: Any) -> None:
        """Insert nodes."""

    @llm_token_counter("insert")
    def insert(self, document: Document, **insert_kwargs: Any) -> None:
        """Insert a document.

        Args:
            document (Union[BaseDocument, BaseGPTIndex]): document to insert

        """
        nodes = self._node_parser.get_nodes_from_documents([document])
        self.docstore.add_documents(nodes)
        self._insert(nodes, **insert_kwargs)

    @abstractmethod
    def _delete(self, doc_id: str, **delete_kwargs: Any) -> None:
        """Delete a document."""

    def delete(self, doc_id: str, **delete_kwargs: Any) -> None:
        """Delete a document from the index.

        All nodes in the index related to the index will be deleted.

        Args:
            doc_id (str): document id

        """
        logger.debug(f"> Deleting document: {doc_id}")
        self._delete(doc_id, **delete_kwargs)

    def update(self, document: Document, **update_kwargs: Any) -> None:
        """Update a document.

        This is equivalent to deleting the document and then inserting it again.

        Args:
            document (Union[BaseDocument, BaseGPTIndex]): document to update
            insert_kwargs (Dict): kwargs to pass to insert
            delete_kwargs (Dict): kwargs to pass to delete

        """
        self.delete(document.get_doc_id(), **update_kwargs.pop("delete_kwargs", {}))
        self.insert(document, **update_kwargs.pop("insert_kwargs", {}))

    def refresh(
        self, documents: Sequence[Document], **update_kwargs: Any
    ) -> List[bool]:
        """Refresh an index with documents that have changed.

        This allows users to save LLM and Embedding model calls, while only
        updating documents that have any changes in text or extra_info. It
        will also insert any documents that previously were not stored.
        """
        refreshed_documents = [False] * len(documents)
        for i, document in enumerate(documents):
            existing_doc_hash = self._docstore.get_document_hash(document.get_doc_id())
            if existing_doc_hash != document.get_doc_hash():
                self.update(document, **update_kwargs)
                refreshed_documents[i] = True
            elif existing_doc_hash is None:
                self.insert(document, **update_kwargs.pop("insert_kwargs", {}))
                refreshed_documents[i] = True

        return refreshed_documents

    def _preprocess_query(self, mode: QueryMode, query_kwargs: Dict) -> None:
        """Preprocess query.

        This allows subclasses to pass in additional query kwargs
        to query, for instance arguments that are shared between the
        index and the query class. By default, this does nothing.
        This also allows subclasses to do validation.

        """
        pass

    def query(
        self,
        query_str: Union[str, QueryBundle],
        mode: str = QueryMode.DEFAULT,
        query_transform: Optional[BaseQueryTransform] = None,
        use_async: bool = False,
        **query_kwargs: Any,
    ) -> Response:
        """Answer a query.

        When `query` is called, we query the index with the given `mode` and
        `query_kwargs`. The `mode` determines the type of query to run, and
        `query_kwargs` are parameters that are specific to the query type.

        For a comprehensive documentation of available `mode` and `query_kwargs` to
        query a given index, please visit :ref:`Ref-Query`.


        """
        mode_enum = QueryMode(mode)
        if mode_enum == QueryMode.RECURSIVE:
            # TODO: deprecated, use ComposableGraph instead.
            if "query_configs" not in query_kwargs:
                raise ValueError("query_configs must be provided for recursive mode.")
            query_configs = query_kwargs["query_configs"]
            query_runner = QueryRunner(
                self._llm_predictor,
                self._prompt_helper,
                self._embed_model,
                self._docstore,
                query_configs=query_configs,
                query_transform=query_transform,
                recursive=True,
                use_async=use_async,
            )
            return query_runner.query(query_str, self._index_struct)
        else:
            self._preprocess_query(mode_enum, query_kwargs)
            # TODO: pass in query config directly
            query_config = QueryConfig(
                index_struct_type=self._index_struct.get_type(),
                query_mode=mode_enum,
                query_kwargs=query_kwargs,
            )
            query_runner = QueryRunner(
                self._llm_predictor,
                self._prompt_helper,
                self._embed_model,
                self._docstore,
                query_configs=[query_config],
                query_transform=query_transform,
                recursive=False,
                use_async=use_async,
            )
            return query_runner.query(query_str, self._index_struct)

    async def aquery(
        self,
        query_str: Union[str, QueryBundle],
        mode: str = QueryMode.DEFAULT,
        query_transform: Optional[BaseQueryTransform] = None,
        **query_kwargs: Any,
    ) -> Response:
        """Asynchronously answer a query.

        When `query` is called, we query the index with the given `mode` and
        `query_kwargs`. The `mode` determines the type of query to run, and
        `query_kwargs` are parameters that are specific to the query type.

        For a comprehensive documentation of available `mode` and `query_kwargs` to
        query a given index, please visit :ref:`Ref-Query`.


        """
        # TODO: currently we don't have async versions of all
        # underlying functions. Setting use_async=True
        # will cause async nesting errors because we assume
        # it's called in a synchronous setting.
        use_async = False

        mode_enum = QueryMode(mode)
        if mode_enum == QueryMode.RECURSIVE:
            # TODO: deprecated, use ComposableGraph instead.
            if "query_configs" not in query_kwargs:
                raise ValueError("query_configs must be provided for recursive mode.")
            query_configs = query_kwargs["query_configs"]
            query_runner = QueryRunner(
                self._llm_predictor,
                self._prompt_helper,
                self._embed_model,
                self._docstore,
                query_configs=query_configs,
                query_transform=query_transform,
                recursive=True,
                use_async=use_async,
            )
            return await query_runner.aquery(query_str, self._index_struct)
        else:
            self._preprocess_query(mode_enum, query_kwargs)
            # TODO: pass in query config directly
            query_config = QueryConfig(
                index_struct_type=self._index_struct.get_type(),
                query_mode=mode_enum,
                query_kwargs=query_kwargs,
            )
            query_runner = QueryRunner(
                self._llm_predictor,
                self._prompt_helper,
                self._embed_model,
                self._docstore,
                query_configs=[query_config],
                query_transform=query_transform,
                recursive=False,
                use_async=use_async,
            )
            return await query_runner.aquery(query_str, self._index_struct)

    @classmethod
    @abstractmethod
    def get_query_map(cls) -> QueryMap:
        """Get query map."""

    @classmethod
    def load_from_dict(
        cls, result_dict: Dict[str, Any], **kwargs: Any
    ) -> "BaseGPTIndex":
        """Load index from dict."""
        if "index_struct" in result_dict:
            index_struct = cls.index_struct_cls.from_dict(result_dict["index_struct"])
            index_struct_id = index_struct.get_doc_id()
        elif "index_struct_id" in result_dict:
            index_struct_id = result_dict["index_struct_id"]
        else:
            raise ValueError("index_struct or index_struct_id must be provided.")

        # NOTE: index_struct can have multiple types for backwards compatibility,
        # map to same class
        type_to_struct = {
            index_type: cls.index_struct_cls
            for index_type in cls.index_struct_cls.get_types()
        }
        type_to_struct[Node.get_type()] = Node

        docstore = DocumentStore.load_from_dict(
            result_dict["docstore"],
            type_to_struct=type_to_struct,
        )
        if "index_struct_id" in result_dict:
            index_struct = docstore.get_document(index_struct_id)
        return cls(index_struct=index_struct, docstore=docstore, **kwargs)

    @classmethod
    def load_from_string(cls, index_string: str, **kwargs: Any) -> "BaseGPTIndex":
        """Load index from string (in JSON-format).

        This method loads the index from a JSON string. The index data
        structure itself is preserved completely. If the index is defined over
        subindices, those subindices will also be preserved (and subindices of
        those subindices, etc.).

        NOTE: load_from_string should not be used for indices composed on top
        of other indices. Please define a `ComposableGraph` and use
        `save_to_string` and `load_from_string` on that instead.

        Args:
            index_string (str): The index string (in JSON-format).

        Returns:
            BaseGPTIndex: The loaded index.

        """
        result_dict = json.loads(index_string)
        return cls.load_from_dict(result_dict, **kwargs)

    @classmethod
    def load_from_disk(cls, save_path: str, **kwargs: Any) -> "BaseGPTIndex":
        """Load index from disk.

        This method loads the index from a JSON file stored on disk. The index data
        structure itself is preserved completely. If the index is defined over
        subindices, those subindices will also be preserved (and subindices of
        those subindices, etc.).

        NOTE: load_from_disk should not be used for indices composed on top
        of other indices. Please define a `ComposableGraph` and use
        `save_to_disk` and `load_from_disk` on that instead.

        Args:
            save_path (str): The save_path of the file.

        Returns:
            BaseGPTIndex: The loaded index.

        """
        with open(save_path, "r") as f:
            file_contents = f.read()
            return cls.load_from_string(file_contents, **kwargs)

    def save_to_dict(self, **save_kwargs: Any) -> dict:
        """Save to dict."""
        if self.docstore.contains_index_struct(
            exclude_ids=[self.index_struct.get_doc_id()], exclude_types=[Node]
        ):
            raise ValueError(
                "Cannot call save index if index is composed on top of "
                "other indices. Please define a `ComposableGraph` and use "
                "`save_to_string` and `load_from_string` on that instead."
            )
        out_dict: Dict[str, Any] = {
            "index_struct_id": self.index_struct.get_doc_id(),
            "docstore": self.docstore.serialize_to_dict(),
        }
        return out_dict

    def save_to_string(self, **save_kwargs: Any) -> str:
        """Save to string.

        This method stores the index into a JSON string.

        NOTE: save_to_string should not be used for indices composed on top
        of other indices. Please define a `ComposableGraph` and use
        `save_to_string` and `load_from_string` on that instead.

        Returns:
            str: The JSON string of the index.

        """
        out_dict = self.save_to_dict(**save_kwargs)
        return json.dumps(out_dict, **save_kwargs)

    def save_to_disk(
        self, save_path: str, encoding: str = "ascii", **save_kwargs: Any
    ) -> None:
        """Save to file.

        This method stores the index into a JSON file stored on disk.

        NOTE: save_to_disk should not be used for indices composed on top
        of other indices. Please define a `ComposableGraph` and use
        `save_to_disk` and `load_from_disk` on that instead.

        Args:
            save_path (str): The save_path of the file.
            encoding (str): The encoding of the file.

        """
        index_string = self.save_to_string(**save_kwargs)
        with open(save_path, "wt", encoding=encoding) as f:
            f.write(index_string)
