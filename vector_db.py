import os
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from dotenv import load_dotenv

load_dotenv()

# Configuration
FAISS_INDEX_PATH = "faiss_index"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

class VectorDB:
    def __init__(self):
        # This will download the model (once) and run it locally
        self.embeddings = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL
        )
        self.db = self._load_or_create_index()

    def _load_or_create_index(self):
        """Load existing FAISS index or create a new empty one."""
        if os.path.exists(FAISS_INDEX_PATH):
            try:
                print(f"Loading existing FAISS index from {FAISS_INDEX_PATH}")
                return FAISS.load_local(FAISS_INDEX_PATH, self.embeddings, allow_dangerous_deserialization=True)
            except Exception as e:
                print(f"Error loading FAISS index: {e}. Creating new one.")
        
        # Create an initial empty index (FAISS needs at least one doc to initialize)
        print("Creating new FAISS index")
        return None

    def add_documents(self, texts, metadatas=None):
        """
        Add documents to the FAISS database.
        texts: List of strings
        metadatas: List of dictionaries
        """
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=100
        )
        
        docs = text_splitter.create_documents(texts, metadatas=metadatas)
        
        if self.db is None:
            self.db = FAISS.from_documents(docs, self.embeddings)
        else:
            self.db.add_documents(docs)
            
        # Save the index locally
        self.db.save_local(FAISS_INDEX_PATH)
        print(f"Added {len(docs)} chunks to FAISS index and saved to {FAISS_INDEX_PATH}")

    def search(self, query, k=3, filter=None):
        """
        Search for relevant documents.
        filter: e.g. {"module": "insurance"}
        """
        if self.db is None:
            return []
            
        # FAISS in LangChain supports filtering via the search method
        results = self.db.similarity_search(query, k=k, filter=filter)
        return results

    def clear_all(self):
        """Clear the entire FAISS index."""
        if os.path.exists(FAISS_INDEX_PATH):
            import shutil
            shutil.rmtree(FAISS_INDEX_PATH)
        self.db = None
        print("Cleared entire FAISS index.")

# Create singleton instance
vector_service = VectorDB()
